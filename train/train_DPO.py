#!/usr/bin/env python
# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import random
import sys
import os
# os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "max_split_size_mb:128"
import argparse
import torch
import transformers
from transformers import AutoModelForCausalLM, set_seed
from datasets import DatasetDict
from alignment import (
    DataArguments,
    DPOConfig,
    H4ArgumentParser,
    ModelArguments,
    apply_chat_template,
    decontaminate_humaneval,
    get_checkpoint,
    get_datasets,
    get_kbit_device_map,
    get_peft_config,
    get_quantization_config,
    get_tokenizer,
    is_adapter_model,
)
from alignment.data import is_openai_format
from peft import PeftConfig, PeftModel
from trl import DPOTrainer

logger = logging.getLogger(__name__)

import pandas as pd
from sklearn.model_selection import train_test_split
import datasets

def build_user_prompt(row):
    return (
        "You are an advanced reasoning agent that can improve based on self-reflection. "
        "You will be given a previous reasoning trial in which you were given a question to answer. "
        "You were unsuccessful in answering the question. In a few sentences, Diagnose a possible reason for failure and devise a new, concise, high-level plan that aims to mitigate the same failure. Use complete sentences.\n\n"
        f"Question: {row['question']}\n"
        f"Previous trial and your incorrect solution: {row['first_trial_reasoning']}"
    )


def main():
    extra_parser = argparse.ArgumentParser()
    extra_parser.add_argument("--data_path", type=str, required=True, help="Path to JSONL data file")
    extra_args, remaining_argv = extra_parser.parse_known_args()
    sys.argv = [sys.argv[0]] + remaining_argv
    parser = H4ArgumentParser((ModelArguments, DataArguments, DPOConfig))
    model_args, data_args, training_args = parser.parse()

    #######
    # Setup
    #######
    logging.basicConfig(
        format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log_level = training_args.get_process_log_level()
    logger.setLevel(log_level)
    transformers.utils.logging.set_verbosity(log_level)
    transformers.utils.logging.enable_default_handler()
    transformers.utils.logging.enable_explicit_format()

    # Log on each process the small summary:
    logger.info(f"Model parameters {model_args}")
    logger.info(f"Data parameters {data_args}")
    logger.info(f"Training/evaluation parameters {training_args}")

    # Check for last checkpoint
    last_checkpoint = get_checkpoint(training_args)
    if last_checkpoint is not None and training_args.resume_from_checkpoint is None:
        logger.info(f"Checkpoint detected, resuming training at {last_checkpoint=}.")

    # Set seed for reproducibility
    set_seed(training_args.seed)

    ###############
    # Load datasets
    ###############
    
    data_path = extra_args.data_path
    df = pd.read_json(data_path, lines=True)

    # reformat
    formatted_data = []
    for _, row in df.iterrows():
        user_input = build_user_prompt(row)
        formatted_data.append({
            "prompt": user_input,
            "chosen": [
                {"content": user_input, "role": "user"},
                {"content": row["reflection_chosen"], "role": "assistant"}
            ],
            "rejected": [
                {"content": user_input, "role": "user"},
                {"content": row["reflection_rejected"], "role": "assistant"}
            ]
        })
        
    df_formatted = pd.DataFrame(formatted_data)
    train_df, test_df = train_test_split(df_formatted, test_size=0.1, random_state=42)
    train_dataset = datasets.Dataset.from_pandas(train_df)
    test_dataset = datasets.Dataset.from_pandas(test_df)
    raw_datasets = DatasetDict({
        'train': train_dataset,
        'test': test_dataset
    })
    
    '''
    raw_datasets = get_datasets(
        data_args,
        splits=data_args.dataset_splits,
        configs=data_args.dataset_configs,
        columns_to_keep=["messages", "chosen", "rejected", "prompt", "completion", "label"],
    )
    '''
    logger.info(
        f"Training on the following splits: {[split + ' : ' + str(dset.num_rows) for split, dset in raw_datasets.items()]}"
    )
    column_names = list(raw_datasets["train"].features)

    #####################################
    # Load tokenizer and process datasets
    #####################################
    data_args.truncation_side = "left"  # Truncate from left to ensure we don't lose labels in final turn
    tokenizer = get_tokenizer(model_args, data_args)

    #####################
    # Apply chat template
    #####################
    raw_datasets = raw_datasets.map(
        apply_chat_template,
        fn_kwargs={
            "tokenizer": tokenizer,
            "task": "dpo",
            "auto_insert_empty_system_msg": data_args.auto_insert_empty_system_msg,
        },
        num_proc=data_args.preprocessing_num_workers,
        desc="Formatting comparisons with prompt template",
    )

    ##########################
    # Decontaminate benchmarks
    ##########################
    # num_raw_train_samples = len(raw_datasets["train"])
    # raw_datasets = raw_datasets.filter(
    #     decontaminate_humaneval,
    #     fn_kwargs={"text_column": "text_chosen"},
    #     batched=True,
    #     batch_size=10_000,
    #     num_proc=1,
    #     desc="Decontaminating HumanEval samples",
    # )
    # num_filtered_train_samples = num_raw_train_samples - len(raw_datasets["train"])
    # logger.info(
    #     f"Decontaminated {num_filtered_train_samples} ({num_filtered_train_samples/num_raw_train_samples * 100:.2f}%) samples from the training set."
    # )

    # Replace column names with what TRL needs, text_chosen -> chosen and text_rejected -> rejected
    '''
    for split in ["train", "test"]:
        raw_datasets[split] = raw_datasets[split].rename_columns(
            {"text_prompt": "prompt", "text_chosen": "chosen", "text_rejected": "rejected"}
        )
    '''
        
    # Log a few random samples from the training set:
    
    for index in random.sample(range(len(raw_datasets["train"])), 3):
        sample = raw_datasets['train'][index]
        logger.info(f"Chosen:\n{sample['chosen']}\n\nRejected:\n{sample['rejected']}")
        '''
        logger.info(f"Chosen sample {index} of the raw training set:\n\n{raw_datasets['train'][index]['chosen']}")
        logger.info(f"Rejected sample {index} of the raw training set:\n\n{raw_datasets['train'][index]['rejected']}")
    for index in random.sample(range(len(raw_datasets["train"])), 3):
        print(f"Sample {index} keys: {raw_datasets['train'][index].keys()}")
        print(f"Sample {index} full content: {raw_datasets['train'][index]}")
    '''
    torch_dtype = (
        model_args.torch_dtype if model_args.torch_dtype in ["auto", None] else getattr(torch, model_args.torch_dtype)
    )
    quantization_config = get_quantization_config(model_args)

    model_kwargs = dict(
        revision=model_args.model_revision,
        trust_remote_code=model_args.trust_remote_code,
        #use_flash_attention_2=model_args.use_flash_attention_2,
        torch_dtype=torch_dtype,
        use_cache=False if training_args.gradient_checkpointing else True,
        # device_map=get_kbit_device_map() if quantization_config is not None else None,
        quantization_config=quantization_config,
        attn_implementation="eager" if "gemma" in model_args.model_name_or_path else "flash_attention_2",
    )

    model = model_args.model_name_or_path
    # if is_adapter_model(model, model_args.model_revision) is True:
    #     logger.info(f"Loading SFT adapter for {model_args.model_name_or_path=}")
    #     peft_config = PeftConfig.from_pretrained(model_args.model_name_or_path, revision=model_args.model_revision)
    #     model_kwargs = dict(
    #         revision=model_args.base_model_revision,
    #         trust_remote_code=model_args.trust_remote_code,
    #         use_flash_attention_2=model_args.use_flash_attention_2,
    #         torch_dtype=torch_dtype,
    #         use_cache=False if training_args.gradient_checkpointing else True,
    #         device_map=get_kbit_device_map() if quantization_config is not None else None,
    #         quantization_config=quantization_config,
    #     )
    #     base_model = AutoModelForCausalLM.from_pretrained(
    #         peft_config.base_model_name_or_path,
    #         **model_kwargs,
    #     )
    #     model = PeftModel.from_pretrained(
    #         base_model,
    #         model_args.model_name_or_path,
    #         revision=model_args.model_revision,
    #     )
    #     model_kwargs = None

    ref_model = model
    ref_model_kwargs = model_kwargs

    if model_args.use_peft is True:
        ref_model = None
        ref_model_kwargs = None

    #########################
    # Instantiate DPO trainer
    #########################
    trainer = DPOTrainer(
        model,
        ref_model,
        model_init_kwargs=model_kwargs,
        ref_model_init_kwargs=ref_model_kwargs,
        args=training_args,
        beta=training_args.beta,
        train_dataset=raw_datasets["train"],
        eval_dataset=raw_datasets["test"],
        tokenizer=tokenizer,
        max_length=training_args.max_length,
        max_prompt_length=training_args.max_prompt_length,
        peft_config=get_peft_config(model_args),
        loss_type=training_args.loss_type,
    )

    ###############
    # Training loop
    ###############
    checkpoint = None
    if training_args.resume_from_checkpoint is not None:
        checkpoint = training_args.resume_from_checkpoint
    elif last_checkpoint is not None:
        checkpoint = last_checkpoint
    train_result = trainer.train(resume_from_checkpoint=checkpoint)
    metrics = train_result.metrics
    metrics["train_samples"] = len(raw_datasets["train"])
    trainer.log_metrics("train", metrics)
    trainer.save_metrics("train", metrics)
    trainer.save_state()

    logger.info("*** Training complete ***")

    ##################################
    # Save model and create model card
    ##################################
    logger.info("*** Save model ***")
    trainer.save_model(training_args.output_dir)
    logger.info(f"Model saved to {training_args.output_dir}")

    # Save everything else on main process
    kwargs = {
        "finetuned_from": model_args.model_name_or_path,
        "dataset": list(data_args.dataset_mixer.keys()),
        "dataset_tags": list(data_args.dataset_mixer.keys()),
        "tags": ["alignment-handbook"],
    }
    if trainer.accelerator.is_main_process:
        trainer.create_model_card(**kwargs)
        # Restore k,v cache for fast inference
        trainer.model.config.use_cache = True
        trainer.model.config.save_pretrained(training_args.output_dir)

    ##########
    # Evaluate
    ##########
    if training_args.do_eval:
        logger.info("*** Evaluate ***")
        metrics = trainer.evaluate()
        metrics["eval_samples"] = len(raw_datasets["test"])
        trainer.log_metrics("eval", metrics)
        trainer.save_metrics("eval", metrics)

    if training_args.push_to_hub is True:
        logger.info("Pushing to hub...")
        trainer.push_to_hub(**kwargs)

    logger.info("*** Training complete! ***")


if __name__ == "__main__":
    main()
