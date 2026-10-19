"""Training subprocess: LoRA / QLoRA with Hugging Face Transformers + PEFT. Launched ONLY by TrainingManager.start() after
a human approved the run. Writes progress.json, train.log (stdout), checkpoints, adapter/ and metrics.json. Never writes to
the base model folder.

Uses transformers.Trainer (stable across versions). TRL's SFTTrainer is a drop-in alternative for the same data format.
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main(cfg_path: str) -> int:
    cfg = json.loads(Path(cfg_path).read_text(encoding="utf-8"))
    out = Path(cfg["output_dir"])
    import torch
    import transformers
    from peft import LoraConfig, TaskType, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer, Trainer, TrainerCallback, TrainingArguments

    log(f"torch {torch.__version__} transformers {transformers.__version__} cuda={torch.cuda.is_available()}")
    transformers.set_seed(int(cfg["seed"]))
    tok = AutoTokenizer.from_pretrained(cfg["base_model"], local_files_only=True)
    if tok.pad_token is None:
        tok.pad_token = tok.eos_token
    cuda = torch.cuda.is_available()
    kwargs: dict = {"local_files_only": True}
    if cfg["method"] == "qlora":
        if not cuda:
            log("QLoRA needs a CUDA GPU + bitsandbytes")
            return 2
        from transformers import BitsAndBytesConfig
        kwargs["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16)
        kwargs["device_map"] = "auto"
    elif cuda:
        kwargs["torch_dtype"] = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    model = AutoModelForCausalLM.from_pretrained(cfg["base_model"], **kwargs)
    if cfg["method"] == "qlora":
        from peft import prepare_model_for_kbit_training
        model = prepare_model_for_kbit_training(model)
    lcfg = LoraConfig(r=cfg["rank"], lora_alpha=cfg["alpha"], lora_dropout=cfg["dropout"], target_modules=cfg["target_modules"],
                      task_type=TaskType.CAUSAL_LM, bias="none")
    model = get_peft_model(model, lcfg)
    model.print_trainable_parameters()

    max_len = int(cfg["max_seq_len"])

    def encode(messages: list[dict]) -> dict:
        prompt_msgs, answer = messages[:-1], messages[-1]["content"]
        if getattr(tok, "chat_template", None):
            prompt = tok.apply_chat_template(prompt_msgs, tokenize=False, add_generation_prompt=True)
            full = prompt + answer + (tok.eos_token or "")
        else:
            prompt = "".join(f"### {m['role'].capitalize()}:\n{m['content']}\n\n" for m in prompt_msgs) + "### Assistant:\n"
            full = prompt + answer + (tok.eos_token or "")
        p_ids = tok(prompt, add_special_tokens=False)["input_ids"]
        f_ids = tok(full, add_special_tokens=False)["input_ids"][:max_len]
        labels = [-100] * min(len(p_ids), len(f_ids)) + f_ids[len(p_ids):]       # train on the answer only
        return {"input_ids": f_ids, "attention_mask": [1] * len(f_ids), "labels": labels}

    def load(split: str) -> list[dict]:
        p = Path(cfg["dataset_dir"]) / f"{split}.jsonl"
        return [encode(json.loads(l)["messages"]) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]

    train_ds, val_ds, test_ds = load("train"), load("val"), load("test")
    log(f"examples: train={len(train_ds)} val={len(val_ds)} test={len(test_ds)}")

    def collate(batch: list[dict]) -> dict:
        n = max(len(b["input_ids"]) for b in batch)
        pad = tok.pad_token_id
        return {"input_ids": torch.tensor([b["input_ids"] + [pad] * (n - len(b["input_ids"])) for b in batch]),
                "attention_mask": torch.tensor([b["attention_mask"] + [0] * (n - len(b["attention_mask"])) for b in batch]),
                "labels": torch.tensor([b["labels"] + [-100] * (n - len(b["labels"])) for b in batch])}

    class Progress(TrainerCallback):
        def on_log(self, args, state, control, logs=None, **kw):
            (out / "progress.json").write_text(json.dumps({"step": state.global_step, "max_steps": state.max_steps, "epoch": state.epoch, "logs": logs, "ts": time.time()}))
            log(f"step {state.global_step}/{state.max_steps} {logs}")

        def on_step_end(self, args, state, control, **kw):
            if (out / "STOP").exists():
                log("STOP file found: stopping gracefully")
                control.should_training_stop = True

    args = TrainingArguments(output_dir=str(out / "checkpoints"), per_device_train_batch_size=int(cfg["batch_size"]), per_device_eval_batch_size=int(cfg["batch_size"]),
                             gradient_accumulation_steps=int(cfg["grad_accum"]), learning_rate=float(cfg["learning_rate"]), num_train_epochs=float(cfg["epochs"]),
                             max_steps=int(cfg["max_steps"]), logging_steps=int(cfg["logging_steps"]), save_steps=int(cfg["save_steps"]), save_total_limit=2,
                             report_to=[], remove_unused_columns=False, bf16=cuda and torch.cuda.is_bf16_supported(), fp16=cuda and not torch.cuda.is_bf16_supported(),
                             use_cpu=not cuda, seed=int(cfg["seed"]), gradient_checkpointing=False)
    trainer = Trainer(model=model, args=args, train_dataset=train_ds, eval_dataset=val_ds, data_collator=collate, callbacks=[Progress()])

    def eval_loss(ds: list[dict]) -> float | None:
        if not ds:
            return None
        return float(trainer.evaluate(eval_dataset=ds)["eval_loss"])

    metrics: dict = {"n_train": len(train_ds), "n_val": len(val_ds), "n_test": len(test_ds), "config": {k: cfg[k] for k in ("method", "rank", "alpha", "learning_rate", "epochs", "max_steps")}}
    log("evaluating BASELINE (adapter disabled)")
    with model.disable_adapter():
        metrics["baseline_val_loss"], metrics["baseline_test_loss"] = eval_loss(val_ds), eval_loss(test_ds)
    log("training")
    result = trainer.train()
    metrics["train_loss"] = float(result.training_loss)
    metrics["stopped_early"] = (out / "STOP").exists()
    log("evaluating TUNED model")
    metrics["tuned_val_loss"], metrics["tuned_test_loss"] = eval_loss(val_ds), eval_loss(test_ds)
    for k in ("baseline_val_loss", "baseline_test_loss", "tuned_val_loss", "tuned_test_loss"):
        if metrics.get(k) is not None:
            metrics[k.replace("loss", "perplexity")] = float(math.exp(min(metrics[k], 50)))
    model.save_pretrained(str(out / "adapter"))          # adapter weights only - the base model is never written
    tok.save_pretrained(str(out / "adapter"))
    (out / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    log("done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1]))
    except Exception as e:  # noqa: BLE001 - surface the error in the log for the UI
        import traceback
        traceback.print_exc()
        log(f"FAILED: {e.__class__.__name__}: {e}")
        sys.exit(1)
