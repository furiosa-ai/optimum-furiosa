#!/usr/bin/env python
# coding=utf-8
#  Copyright 2023 The HuggingFace Team. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

""" Finetuning the library models for image classification."""
# You can also adapt this script on your own image classification task. Pointers for this are left as comments.
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Optional

import datasets
import numpy as np
import torch
import transformers
from datasets import load_dataset
from evaluate import load
from torchvision.transforms import CenterCrop, Compose, Normalize, Resize, ToTensor
from transformers import AutoConfig, AutoFeatureExtractor, EvalPrediction, HfArgumentParser, TrainingArguments
from transformers.utils.versions import require_version

from optimum.furiosa import FuriosaAIModelForImageClassification, FuriosaAIQuantizer
from optimum.furiosa.configuration import AutoCalibrationConfig, QuantizationConfig
from optimum.furiosa.utils import export_model_to_onnx


logger = logging.getLogger(__name__)

require_version("datasets>=2.0.0", "To fix: pip install -r examples/pytorch/image-classification/requirements.txt")


@dataclass
class DataTrainingArguments:
    """
    Arguments pertaining to what data we are going to input our model for training and eval.

    Using `HfArgumentParser` we can turn this class
    into argparse arguments to be able to specify them on
    the command line.
    """

    dataset_name: Optional[str] = field(
        default=None, metadata={"help": "The name of the dataset to use (via the datasets library)."}
    )
    dataset_config_name: Optional[str] = field(
        default=None, metadata={"help": "The configuration name of the dataset to use (via the datasets library)."}
    )
    max_seq_length: int = field(
        default=128,
        metadata={
            "help": "The maximum total input sequence length after tokenization. Sequences longer "
            "than this will be truncated, sequences shorter will be padded."
        },
    )
    overwrite_cache: bool = field(
        default=False, metadata={"help": "Overwrite the cached preprocessed datasets or not."}
    )
    max_eval_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of evaluation examples to this "
            "value if set."
        },
    )
    max_predict_samples: Optional[int] = field(
        default=None,
        metadata={
            "help": "For debugging purposes or quicker training, truncate the number of prediction examples to this "
            "value if set."
        },
    )
    train_dir: Optional[str] = field(default=None, metadata={"help": "A directory path for the training data."})
    validation_dir: Optional[str] = field(default=None, metadata={"help": "A directory path for the validation data."})


@dataclass
class ModelArguments:
    """
    Arguments pertaining to which model/config/tokenizer we are going to fine-tune from.
    """

    model_name_or_path: str = field(
        metadata={"help": "Path to pretrained model or model identifier from huggingface.co/models"}
    )
    cache_dir: Optional[str] = field(
        default=None,
        metadata={"help": "Where do you want to store the pretrained models downloaded from huggingface.co"},
    )
    batch_size: int = field(
        default=1,
        metadata={"help": "The batch size for the model."},
    )
    num_labels: int = field(
        default=3,
        metadata={"help": "The batch size for the model."},
    )


@dataclass
class OptimizationArguments:
    """
    Arguments pertaining to what type of optimization we are going to apply on the model.
    """

    quantization_approach: str = field(
        default="static",
        metadata={"help": "The quantization approach. Supported approach are static and dynamic."},
    )
    calibration_method: str = field(
        default="minmax_asym",
        metadata={
            "help": "The method chosen to calculate the activation quantization parameters using the calibration "
            "dataset. Current supported calibration methods are minmax, entropy and percentile."
        },
    )
    num_calibration_samples: int = field(
        default=100,
        metadata={"help": "Number of examples to use for the calibration step resulting from static quantization."},
    )
    num_calibration_shards: int = field(
        default=1,
        metadata={
            "help": "How many shards to split the calibration dataset into. Useful for the entropy and percentile "
            "calibration method."
        },
    )
    calibration_batch_size: int = field(
        default=1,
        metadata={"help": "The batch size for the calibration step."},
    )
    calibration_histogram_percentile: float = field(
        default=99.999,
        metadata={"help": "The percentile used for the percentile calibration method."},
    )


def main():
    # We now keep distinct sets of args, for a cleaner separation of concerns.
    parser = HfArgumentParser((ModelArguments, DataTrainingArguments, TrainingArguments, OptimizationArguments))
    if len(sys.argv) == 2 and sys.argv[1].endswith(".json"):
        # If we pass only one argument to the script and it's the path to a json file,
        # let's parse it to get our arguments.
        model_args, data_args, training_args, optim_args, onnx_export_args = parser.parse_json_file(
            json_file=os.path.abspath(sys.argv[1])
        )
    else:
        model_args, data_args, training_args, optim_args = parser.parse_args_into_dataclasses()

    # Setup logging
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%m/%d/%Y %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    datasets.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    logger.info(f"Optimization with the following parameters {optim_args}")

    if os.path.isdir(training_args.output_dir) and not training_args.overwrite_output_dir:
        raise ValueError(
            f"Output directory ({training_args.output_dir}) already exists and is not empty. "
            "Use --overwrite_output_dir to overcome."
        )

    # Sanity checks
    if data_args.dataset_name is None and data_args.train_dir is None and data_args.validation_dir is None:
        raise ValueError("Need either a dataset name or a training/validation folder.")

    os.makedirs(training_args.output_dir, exist_ok=True)

    # Get the datasets: you can either provide your own training and evaluation files (see below)
    # or specify a Dataset from the hub (the dataset will be downloaded automatically from the datasets Hub).
    if data_args.dataset_name is not None:
        # Downloading and loading a dataset from the hub.
        dataset = load_dataset(data_args.dataset_name)
    else:
        data_files = {}
        if data_args.train_dir is not None:
            data_files["train"] = os.path.join(data_args.train_dir, "**")
        if data_args.validation_dir is not None:
            data_files["validation"] = os.path.join(data_args.validation_dir, "**")
        dataset = load_dataset(
            "imagefolder",
            data_files=data_files,
            cache_dir=model_args.cache_dir,
            task="image-classification",
        )
        # See more about loading custom images at
        # https://huggingface.co/docs/datasets/v2.0.0/en/image_process#imagefolder.

    labels_column = (
        "labels" if "labels" in dataset["validation"].column_names else dataset["validation"].column_names[1]
    )

    feature_extractor = AutoFeatureExtractor.from_pretrained(model_args.model_name_or_path)

    # Define torchvision transforms to be applied to each image.
    normalize = Normalize(mean=feature_extractor.image_mean, std=feature_extractor.image_std)
    image_size = feature_extractor.size["shortest_edge"]
    transforms = Compose(
        [
            Resize(image_size),
            CenterCrop(image_size),
            ToTensor(),
            normalize,
        ]
    )

    def preprocess_function(example_batch):
        """Apply transforms across a batch."""
        example_batch["pixel_values"] = [
            transforms(image.convert("RGB")).to(torch.float32).numpy() for image in example_batch["image"]
        ]
        return example_batch

    metric = load("accuracy")

    # You can define your custom compute_metrics function. It takes an `EvalPrediction` object (a namedtuple with a
    # predictions and label_ids field) and has to return a dictionary string to float.
    def compute_metrics(p: EvalPrediction):
        preds = p.predictions[0] if isinstance(p.predictions, tuple) else p.predictions
        preds = np.argmax(preds, axis=1)

        result = metric.compute(predictions=preds, references=p.label_ids)
        return result

    # Export the model
    export_model_to_onnx(
        model_args.model_name_or_path,
        save_dir=training_args.output_dir,
        input_shape_dict={"pixel_values": [model_args.batch_size, 3, image_size, image_size]},
        output_shape_dict={"logits": [model_args.batch_size, model_args.num_labels]},
    )

    # Create the quantizer
    quantizer = FuriosaAIQuantizer.from_pretrained(training_args.output_dir, file_name="model.onnx")

    # Create the quantization configuration containing all the quantization parameters
    qconfig = QuantizationConfig()

    ranges = None

    calibration_dataset = dataset["train"]
    if optim_args.num_calibration_samples is not None:
        calibration_dataset = calibration_dataset.shuffle(seed=training_args.seed).select(
            range(optim_args.num_calibration_samples)
        )

    # all images are loaded in memory, which could prove expensive if num_calibration_samples is large
    calibration_dataset = calibration_dataset.map(
        partial(preprocess_function),
        batched=True,
        load_from_cache_file=not data_args.overwrite_cache,
        desc="Running preprocessing on calibration dataset",
    )

    # Remove the unnecessary columns of the calibration dataset before the calibration step
    calibration_dataset = quantizer.clean_calibration_dataset(calibration_dataset)

    # Create the calibration configuration given the selected calibration method
    if optim_args.calibration_method == "percentile_asym":
        calibration_config = AutoCalibrationConfig.percentiles_asym(
            calibration_dataset,
            percentile=optim_args.calibration_histogram_percentile,
        )
    else:
        calibration_config = AutoCalibrationConfig.minmax_asym(calibration_dataset)

    if not 1 <= optim_args.num_calibration_shards <= len(calibration_dataset):
        raise ValueError(
            f"Invalid value of number of shards {optim_args.num_calibration_shards} chosen to split the calibration"
            f" dataset, should be higher than 0 and lower or equal to the number of samples "
            f"{len(calibration_dataset)}."
        )

    for i in range(optim_args.num_calibration_shards):
        shard = calibration_dataset.shard(optim_args.num_calibration_shards, i)
        quantizer.partial_fit(
            dataset=shard,
            calibration_config=calibration_config,
            batch_size=optim_args.calibration_batch_size,
        )
    ranges = quantizer.compute_ranges()

    # Apply quantization on the model
    quantizer.quantize(
        save_dir=training_args.output_dir,
        calibration_tensors_range=ranges,
        quantization_config=qconfig,
    )

    # Evaluation
    if training_args.do_eval:
        logger.info("*** Evaluate ***")

        model_config = AutoConfig.from_pretrained(model_args.model_name_or_path)
        eval_dataset = dataset["validation"]
        if data_args.max_eval_samples is not None:
            eval_dataset = eval_dataset.shuffle(seed=training_args.seed).select(range(data_args.max_eval_samples))

        try:
            eval_dataset = eval_dataset.align_labels_with_mapping(
                label2id=model_config.label2id, label_column=labels_column
            )
        except Exception:
            logger.warning(
                f"\nModel label mapping: {model_config.label2id}"
                f"\nDataset label features: {eval_dataset.features[labels_column]}"
                f"\nCould not guarantee the model label mapping and the dataset labels match."
                f" Evaluation results may suffer from a wrong matching."
            )

        # Set the validation transforms
        eval_dataset = eval_dataset.with_transform(preprocess_function)

        furiosa_model = FuriosaAIModelForImageClassification(
            Path(training_args.output_dir) / "model_quantized.dfg",
            compute_metrics=compute_metrics,
            label_names=[labels_column],
        )
        outputs = furiosa_model.evaluation_loop(eval_dataset)
        # Save metrics
        with open(os.path.join(training_args.output_dir, "eval_results.json"), "w") as f:
            json.dump(outputs.metrics, f, indent=4, sort_keys=True)


if __name__ == "__main__":
    main()
