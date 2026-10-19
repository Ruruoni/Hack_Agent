"""Merge a LoRA adapter into a COPY of the base weights (step 1 of the LM Studio export workflow).
Usage: python -m nexus.training.merge --base <hf model dir> --adapter <adapter dir> --out <new dir>
The base model directory is only read."""
import argparse
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True)
    ap.add_argument("--adapter", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    if Path(a.out).resolve() == Path(a.base).resolve():
        print("refusing to write into the base model folder", file=sys.stderr)
        return 2
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer
    model = AutoModelForCausalLM.from_pretrained(a.base, torch_dtype=torch.float16, local_files_only=True)
    model = PeftModel.from_pretrained(model, a.adapter).merge_and_unload()
    model.save_pretrained(a.out, safe_serialization=True)
    AutoTokenizer.from_pretrained(a.base, local_files_only=True).save_pretrained(a.out)
    print("merged model written to", a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
