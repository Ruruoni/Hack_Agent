"""Optional model-weight adaptation (LoRA / QLoRA) - fully separate from ingestion and memory.

Guarantees implemented here:
  * Never starts automatically: a run needs a human-approved, single-use approval record.
  * The original model is never modified: training writes a *new adapter directory*; a fingerprint of the base model
    files is taken before the run and re-verified afterwards.
  * Evaluation compares the adapted model with the baseline on a held-out test split that is leak-checked.
  * A human accepts or rejects the result; accepting registers it, activating switches LM Studio's chat model setting, and
    rollback restores the previous setting.
  * If the machine / libraries cannot train, the manager says why and the rest of the app keeps working.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..config import save_overrides
from ..database.db import dumps, new_id, now
from ..ingestion.quality import QualityRules, assess
from ..knowledge.text import canon, hamming, scan_injection, sha, simhash64
from ..system import hardware

SUPPORTED = {  # model_type -> LoRA target modules
    "llama": ["q_proj", "k_proj", "v_proj", "o_proj"], "mistral": ["q_proj", "k_proj", "v_proj", "o_proj"],
    "qwen2": ["q_proj", "k_proj", "v_proj", "o_proj"], "qwen3": ["q_proj", "k_proj", "v_proj", "o_proj"],
    "gemma": ["q_proj", "k_proj", "v_proj", "o_proj"], "gemma2": ["q_proj", "k_proj", "v_proj", "o_proj"],
    "phi3": ["qkv_proj", "o_proj"], "gpt2": ["c_attn"], "gpt_neox": ["query_key_value"], "falcon": ["query_key_value"],
    "opt": ["q_proj", "v_proj"], "gptj": ["q_proj", "v_proj"],
}
GGUF_CONVERTIBLE = {"llama", "mistral", "qwen2", "qwen3", "gemma", "gemma2", "phi3", "gpt2", "gpt_neox", "falcon", "gptj"}
PERMISSIVE = ("mit", "apache-2.0", "bsd", "cc0", "cc-by-4.0", "cc-by-sa-4.0", "odc-by", "user-owned", "own-data", "public-domain", "unlicense")
REQUIRED_LIBS = ("torch", "transformers", "peft")
OPTIONAL_LIBS = ("trl", "datasets", "accelerate", "bitsandbytes")


class TrainingError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


def _have(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def _fingerprint(path: Path) -> dict:
    """Cheap-but-meaningful identity of a model directory: size + sha256 (small files fully, big files head/tail 1 MiB)."""
    out = {}
    for f in sorted(path.rglob("*")):
        if not f.is_file() or f.name.startswith("."):
            continue
        size = f.stat().st_size
        h = hashlib.sha256()
        with open(f, "rb") as fh:
            if size <= 64 * 2**20:
                while b := fh.read(1 << 20):
                    h.update(b)
            else:
                h.update(fh.read(2**20))
                fh.seek(size - 2**20)
                h.update(fh.read(2**20))
                h.update(str(size).encode())
        out[str(f.relative_to(path))] = {"size": size, "sha256": h.hexdigest()}
    return out


class TrainingManager:
    def __init__(self, app):
        self.app = app
        self.db = app.db
        self.s = app.settings
        self._procs: dict[str, subprocess.Popen] = {}

    # ------------------------------------------------------------------ 1. hardware & libraries
    def hardware(self) -> dict:
        hw = self.app.hardware()
        libs = {m: _have(m) for m in REQUIRED_LIBS + OPTIONAL_LIBS}
        cuda = bool(hw["torch"].get("cuda"))
        blockers = []
        if not all(libs[m] for m in REQUIRED_LIBS):
            blockers.append("Training libraries are missing (" + ", ".join(m for m in REQUIRED_LIBS if not libs[m]) +
                            "). Install with: pip install -r backend/requirements-training.txt (choose the right PyTorch build first).")
        if not hw["gpus"] and not cuda:
            blockers.append("No NVIDIA GPU detected: only very small models (<~1B parameters) are practical on CPU.")
        return {**hw, "libraries": libs, "cuda_available": cuda, "blockers": blockers,
                "can_train_here": not blockers or (all(libs[m] for m in REQUIRED_LIBS)),
                "note": "Retrieval and persistent memory keep working regardless of this result."}

    # ------------------------------------------------------------------ 2. model compatibility
    def check_model(self, ref: str) -> dict:
        p = Path(ref).expanduser()
        if not p.exists():
            return {"ref": ref, "format": "hf_repo_id", "trainable": None, "resolved": False,
                    "message": "Not a local folder. Download the Hugging Face-format model first (huggingface-cli download) and pass its folder path, "
                               "or train directly from the repo id on a machine with internet access."}
        if p.is_file() and p.suffix == ".gguf" or (p.is_dir() and list(p.rglob("*.gguf")) and not (p / "config.json").exists()):
            return {"ref": str(p), "format": "gguf", "trainable": False,
                    "message": "This is a GGUF file (LM Studio's inference format). It cannot be fine-tuned with Transformers/PEFT. "
                               "Find the original Hugging Face-format weights of the same model (linked on its model card), fine-tune those, "
                               "then convert the result back to GGUF (see the export workflow)."}
        cfgp = p / "config.json"
        if not cfgp.is_file():
            return {"ref": str(p), "format": "unknown", "trainable": False, "message": "config.json not found: not a Transformers model folder."}
        cfg = json.loads(cfgp.read_text(encoding="utf-8"))
        mt = cfg.get("model_type", "")
        H, L, V = cfg.get("hidden_size") or cfg.get("n_embd") or 0, cfg.get("num_hidden_layers") or cfg.get("n_layer") or 0, cfg.get("vocab_size", 0)
        I = cfg.get("intermediate_size") or 4 * H
        params = int(V * H + L * (4 * H * H + 3 * H * I)) if H and L else None
        weights = [f.name for f in p.iterdir() if f.suffix in (".safetensors", ".bin")]
        tok = [n for n in ("tokenizer.json", "tokenizer.model", "tokenizer_config.json", "vocab.json") if (p / n).exists()]
        tc = (p / "tokenizer_config.json")
        has_template = tc.exists() and "chat_template" in tc.read_text(encoding="utf-8", errors="replace")
        issues = []
        if not weights:
            issues.append("no .safetensors/.bin weight files found")
        if not tok:
            issues.append("no tokenizer files found")
        if mt not in SUPPORTED:
            issues.append(f"model_type '{mt}' has no known LoRA target modules - pass target_modules yourself")
        return {"ref": str(p), "format": "transformers", "model_type": mt, "architectures": cfg.get("architectures"), "hidden_size": H, "layers": L,
                "vocab_size": V, "approx_params": params, "weights": weights, "tokenizer_files": tok, "has_chat_template": has_template,
                "target_modules": SUPPORTED.get(mt), "gguf_convertible": mt in GGUF_CONVERTIBLE, "issues": issues, "trainable": bool(weights) and bool(tok) and mt in SUPPORTED}

    # ------------------------------------------------------------------ 3. dataset preparation
    def prepare_dataset(self, source: str, license: str, permitted_use_confirmed: bool, name: str = "", min_examples: int = 20,
                        max_chars: int = 8000) -> dict:
        lic = (license or "").strip().lower()
        if not permitted_use_confirmed:
            raise TrainingError("license_unconfirmed", "You must confirm you have the right to use this data for fine-tuning (licence/permitted use).")
        if not lic:
            raise TrainingError("license_missing", "State the dataset licence (e.g. mit, apache-2.0, cc-by-4.0, user-owned).")
        warnings = []
        if any(x in lic for x in ("nc", "noncommercial", "non-commercial", "research-only")):
            warnings.append(f"Licence '{license}' looks non-commercial/research-only: make sure your use complies.")
        if not any(lic.startswith(x) or x in lic for x in PERMISSIVE):
            warnings.append(f"Licence '{license}' is not in the permissive list: verify that fine-tuning and redistribution of derived weights are allowed.")
        src = Path(source).expanduser()
        if not src.is_file():
            raise TrainingError("source_missing", f"dataset file not found: {source}")
        stats = {"read": 0, "invalid_format": 0, "empty": 0, "too_long": 0, "low_quality": 0, "possible_injection": 0,
                 "exact_duplicates": 0, "near_duplicates": 0, "kept": 0}
        examples: list[dict] = []
        seen: set[str] = set()
        sims: list[tuple[int, int]] = []
        rules = QualityRules(min_chars=2, structured=True, quarantine_injections=False)
        with open(src, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not line.strip():
                    continue
                stats["read"] += 1
                try:
                    msgs = self._to_messages(json.loads(line))
                except (ValueError, KeyError, TypeError):
                    msgs = None
                if not msgs:
                    stats["invalid_format"] += 1
                    continue
                text = "\n".join(m["content"] for m in msgs)
                if not msgs[-1]["content"].strip() or not any(m["role"] == "user" and m["content"].strip() for m in msgs):
                    stats["empty"] += 1
                    continue
                if len(text) > max_chars:
                    stats["too_long"] += 1
                    continue
                if assess(msgs[-1]["content"], rules)[0] == "reject":
                    stats["low_quality"] += 1
                    continue
                if scan_injection(text):
                    stats["possible_injection"] += 1       # poisoned training data would bake instructions into the weights
                    continue
                key = sha(canon(text))
                if key in seen:
                    stats["exact_duplicates"] += 1
                    continue
                sim = simhash64(text)
                if any(hamming(sim, s) <= 3 for s, _ in sims[-5000:]):
                    stats["near_duplicates"] += 1
                    continue
                seen.add(key)
                sims.append((sim, len(examples)))
                examples.append({"messages": msgs, "_prompt_key": sha(canon(next(m["content"] for m in msgs if m["role"] == "user"))), "_sim": sim})
        stats["kept"] = len(examples)
        if len(examples) < min_examples:
            raise TrainingError("too_few_examples", f"Only {len(examples)} usable examples after cleaning (minimum {min_examples}). Stats: {stats}")
        # deterministic split on the PROMPT hash: the same prompt can never land in two splits
        splits: dict[str, list[dict]] = {"train": [], "val": [], "test": []}
        for ex in examples:
            b = int(ex["_prompt_key"][:8], 16) % 100
            splits["train" if b < 80 else "val" if b < 90 else "test"].append(ex)
        if not splits["val"] or not splits["test"]:     # tiny datasets: carve evaluation sets off the train split deterministically
            splits["train"].sort(key=lambda e: e["_prompt_key"])
            for nm in ("val", "test"):
                if not splits[nm] and len(splits["train"]) > 3:
                    splits[nm].append(splits["train"].pop())
        # leakage guard: no identical prompt/answer and no near-duplicate across splits (offenders are dropped from train)
        leaked = 0
        eval_keys = {e["_prompt_key"] for s in ("val", "test") for e in splits[s]}
        eval_sims = [e["_sim"] for s in ("val", "test") for e in splits[s]]
        clean_train = []
        for e in splits["train"]:
            if e["_prompt_key"] in eval_keys or any(hamming(e["_sim"], s) <= 3 for s in eval_sims):
                leaked += 1
            else:
                clean_train.append(e)
        splits["train"] = clean_train
        did = new_id("tds_")
        out = self.s.path("training", "datasets", did)
        out.mkdir(parents=True, exist_ok=True)
        hashes = {}
        for nm, rows in splits.items():
            fp = out / f"{nm}.jsonl"
            with open(fp, "w", encoding="utf-8") as fh:
                for e in rows:
                    fh.write(json.dumps({"messages": e["messages"]}, ensure_ascii=False) + "\n")
            hashes[nm] = hashlib.sha256(fp.read_bytes()).hexdigest()
        manifest = {"id": did, "name": name or src.stem, "source": str(src), "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(), "license": license,
                    "permitted_use_confirmed": True, "stats": stats, "leakage_removed_from_train": leaked,
                    "splits": {k: len(v) for k, v in splits.items()}, "split_hashes": hashes, "warnings": warnings,
                    "version": hashlib.sha256("".join(hashes.values()).encode()).hexdigest()[:12], "dir": str(out), "created": now()}
        (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        self.db.audit("user", "training.dataset", {"id": did, "kept": stats["kept"], "license": license})
        return manifest

    @staticmethod
    def _to_messages(obj: dict) -> list[dict] | None:
        if isinstance(obj.get("messages"), list):
            msgs = [{"role": str(m["role"]), "content": str(m["content"])} for m in obj["messages"]]
        elif "prompt" in obj and "completion" in obj:
            msgs = [{"role": "user", "content": str(obj["prompt"])}, {"role": "assistant", "content": str(obj["completion"])}]
        elif "instruction" in obj and "output" in obj:
            q = str(obj["instruction"]) + (("\n" + str(obj["input"])) if obj.get("input") else "")
            msgs = [{"role": "user", "content": q}, {"role": "assistant", "content": str(obj["output"])}]
        else:
            return None
        if not msgs or msgs[-1]["role"] != "assistant" or any(m["role"] not in ("system", "user", "assistant") for m in msgs):
            return None
        return msgs

    def dataset(self, did: str) -> dict:
        mp = self.s.path("training", "datasets", did, "manifest.json")
        if not mp.is_file():
            raise TrainingError("not_found", f"training dataset {did} not found")
        return json.loads(mp.read_text(encoding="utf-8"))

    def datasets(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.s.path("training", "datasets").glob("*/manifest.json"))]

    # ------------------------------------------------------------------ 4. estimates
    def estimate(self, model: dict, method: str = "lora", seq_len: int = 512, batch_size: int = 1, rank: int = 16) -> dict:
        P = model.get("approx_params") or 0
        H, L = model.get("hidden_size") or 0, model.get("layers") or 0
        if not P:
            return {"known": False, "message": "Cannot estimate: model size unknown."}
        weights = P * (0.55 if method == "qlora" else 2)
        lora_params = 2 * rank * H * L * max(1, len(model.get("target_modules") or ["q"]))
        optimizer = lora_params * 12                                # fp32 adam states + grads + master weights
        act = batch_size * seq_len * H * L * 2 * (10 if method == "lora" else 6) / 4       # gradient checkpointing assumed
        total = (weights + optimizer + act) * 1.15
        hw = self.hardware()
        vram = max((g["vram_free_mb"] for g in hw["gpus"]), default=0) * 2**20
        ram = hw["ram_available_mb"] * 2**20
        feasible, device, reasons = False, "none", []
        if vram and total <= vram:
            feasible, device = True, "cuda"
        elif vram:
            reasons.append(f"needs ~{total / 2**30:.1f} GiB but the GPU has {vram / 2**30:.1f} GiB free" + (" - try qlora" if method == "lora" else " - try a smaller model or sequence length"))
        if not feasible and P <= 1.6e9 and total * 1.3 <= ram:
            feasible, device = True, "cpu"
            reasons.append("CPU training: works for tiny models but is very slow")
        if not feasible and not reasons:
            reasons.append(f"needs ~{total / 2**30:.1f} GiB; only {ram / 2**30:.1f} GiB RAM and no usable GPU")
        if method == "qlora" and not hw["libraries"]["bitsandbytes"]:
            reasons.append("QLoRA needs bitsandbytes (NVIDIA GPU)")
            feasible = False if device != "cpu" else feasible
        return {"known": True, "params": P, "method": method, "memory_gib": round(total / 2**30, 2), "trainable_lora_params": lora_params,
                "feasible": feasible, "device": device, "reasons": reasons,
                "note": "Rough rule-of-thumb estimate, not a guarantee. Time depends heavily on hardware; expect minutes for tiny models and hours+ for 7B."}

    # ------------------------------------------------------------------ 5. plan / approval / run
    def plan(self, base_model: str, dataset_id: str, method: str = "lora", params: dict | None = None) -> dict:
        model = self.check_model(base_model)
        if model.get("format") != "transformers":
            raise TrainingError("model_incompatible", model.get("message") or "model is not a Transformers-format folder")
        if not model["target_modules"] and not (params or {}).get("target_modules"):
            raise TrainingError("model_incompatible", "; ".join(model["issues"]))
        ds = self.dataset(dataset_id)
        cfg = {"method": method, "rank": 16, "alpha": 32, "dropout": 0.05, "epochs": 1, "max_steps": -1, "learning_rate": 2e-4, "batch_size": 1,
               "grad_accum": 8, "max_seq_len": 512, "seed": 42, "target_modules": model["target_modules"], "logging_steps": 5, "save_steps": 50, **(params or {})}
        est = self.estimate(model, method, cfg["max_seq_len"], cfg["batch_size"], cfg["rank"])
        rid = new_id("tr_")
        out = self.s.path("training", "runs", rid)
        out.mkdir(parents=True, exist_ok=True)
        fp = _fingerprint(Path(model["ref"]))
        (out / "base_fingerprint.json").write_text(json.dumps(fp), encoding="utf-8")
        config = {"run_id": rid, "base_model": model["ref"], "dataset_dir": ds["dir"], "output_dir": str(out), **cfg}
        (out / "config.json").write_text(json.dumps(config, indent=2), encoding="utf-8")
        self.db.insert("training_runs", {"id": rid, "base_model": model["ref"], "status": "planned", "config": config, "hardware": self.hardware(),
                                         "dataset": {k: ds[k] for k in ("id", "name", "version", "license", "splits", "stats")}, "estimate": est, "metrics": {},
                                         "log_path": str(out / "train.log"), "output_dir": str(out), "created": now(), "updated": now()})
        self.db.audit("user", "training.plan", {"run": rid, "base": model["ref"], "dataset": dataset_id})
        return self.get(rid)  # type: ignore[return-value]

    def get(self, rid: str) -> dict | None:
        r = self.db.get("training_runs", rid)
        if r:
            pj = Path(r["output_dir"]) / "progress.json"
            r["progress"] = json.loads(pj.read_text()) if pj.is_file() else None
            r["live"] = rid in self._procs and self._procs[rid].poll() is None
        return r

    def list(self) -> list[dict]:
        return [self.get(r["id"]) for r in self.db.query("SELECT id FROM training_runs ORDER BY created DESC")]  # type: ignore[misc]

    def request_approval(self, rid: str) -> str:
        r = self.db.get("training_runs", rid)
        if not r or r["status"] != "planned":
            raise TrainingError("bad_state", "run must be in 'planned' state")
        est = r["estimate"]
        if est.get("known") and not est.get("feasible"):
            raise TrainingError("not_feasible", "This machine cannot reasonably train this model: " + "; ".join(est.get("reasons", [])) +
                                ". Retrieval and memory still work; pick a smaller model or different hardware.")
        aid = self.app.perms.request_approval(None, "fine_tune", {"run": rid}, "critical", f"Fine-tune {Path(r['base_model']).name} on {r['dataset']['name']} ({r['dataset']['splits']})", kind="training")
        self.db.update("training_runs", rid, {"status": "awaiting_approval", "decision": aid, "updated": now()})
        return aid

    def start(self, rid: str) -> dict:
        r = self.db.get("training_runs", rid)
        if not r or r["status"] != "awaiting_approval":
            raise TrainingError("bad_state", "request approval first")
        aid = r["decision"]
        a = self.db.get("approvals", aid)
        if not a or a["status"] != "approved":
            raise TrainingError("not_approved", f"approval is '{a['status'] if a else 'missing'}': a human must approve training explicitly")
        if not self.app.perms.consume(aid, "fine_tune", {"run": rid}):
            raise TrainingError("not_approved", "approval is invalid or already used")
        missing = [m for m in REQUIRED_LIBS if not _have(m)]
        if missing:
            self.db.update("training_runs", rid, {"status": "blocked_dependencies", "updated": now(),
                           "metrics": {"error": f"missing libraries: {missing}"}})
            raise TrainingError("missing_dependencies", f"Missing: {missing}. Run: pip install -r backend/requirements-training.txt (install the right PyTorch build first).")
        out = Path(r["output_dir"])
        log = open(out / "train.log", "ab")
        env = {k: v for k, v in os.environ.items() if k not in ("HF_TOKEN", "HUGGINGFACE_HUB_TOKEN") or r["config"].get("use_hf_token")}
        p = subprocess.Popen([sys.executable, "-m", "nexus.training.runner", str(out / "config.json")], stdout=log, stderr=subprocess.STDOUT,
                             cwd=str(Path(__file__).resolve().parents[2]), env=env)
        self._procs[rid] = p
        self.db.update("training_runs", rid, {"status": "running", "updated": now()})
        self.db.audit("user", "training.start", {"run": rid, "pid": p.pid})
        threading.Thread(target=self._watch, args=(rid, p), daemon=True).start()
        return self.get(rid)  # type: ignore[return-value]

    def _watch(self, rid: str, p: subprocess.Popen) -> None:
        p.wait()
        self.finalize(rid, exit_code=p.returncode)

    def stop(self, rid: str) -> None:
        p = self._procs.get(rid)
        out = Path(self.db.get("training_runs", rid)["output_dir"])
        (out / "STOP").write_text("stop")                # graceful: runner checks this file each step
        if p and p.poll() is None:
            threading.Timer(30, lambda: p.poll() is None and p.kill()).start()

    def finalize(self, rid: str, exit_code: int | None = None) -> dict:
        r = self.db.get("training_runs", rid)
        out = Path(r["output_dir"])
        mp = out / "metrics.json"
        if exit_code not in (0, None) or not mp.is_file():
            tail = self.log_tail(rid, 15)
            self.db.update("training_runs", rid, {"status": "failed" if not (out / "STOP").exists() else "stopped", "updated": now(),
                                                  "metrics": {"error": f"runner exit code {exit_code}", "log_tail": tail}})
            return self.get(rid)  # type: ignore[return-value]
        return self.record_metrics(rid, json.loads(mp.read_text()))

    def record_metrics(self, rid: str, m: dict) -> dict:
        """Compare adapted vs baseline. Loss is an imperfect proxy: a human still decides."""
        r = self.db.get("training_runs", rid)
        cmp: dict[str, Any] = {}
        for split in ("val", "test"):
            b, t = m.get(f"baseline_{split}_loss"), m.get(f"tuned_{split}_loss")
            if b is not None and t is not None:
                cmp[split] = {"baseline_loss": b, "tuned_loss": t, "delta": round(t - b, 5), "relative_change_pct": round(100 * (t - b) / b, 2) if b else None,
                              "verdict": "improved" if t < b * 0.99 else "regressed" if t > b * 1.02 else "no significant change"}
        regress = [s for s, c in cmp.items() if c["verdict"] == "regressed"]
        problems = self.verify_base_unchanged(rid)
        verdict = "regressed" if regress else "improved" if cmp and all(c["verdict"] == "improved" for c in cmp.values()) else "mixed / inconclusive"
        full = {**m, "comparison": cmp, "overall": verdict, "regressions": regress, "base_model_unchanged": not problems, "base_problems": problems,
                "caveat": "Loss on held-out examples is only a proxy. Test the exported model on your own prompts in LM Studio before relying on it."}
        self.db.update("training_runs", rid, {"status": "awaiting_decision", "metrics": full, "updated": now()})
        return self.get(rid)  # type: ignore[return-value]

    def verify_base_unchanged(self, rid: str) -> list[str]:
        r = self.db.get("training_runs", rid)
        fp_path = Path(r["output_dir"]) / "base_fingerprint.json"
        if not fp_path.is_file():
            return ["no base fingerprint recorded"]
        before = json.loads(fp_path.read_text())
        after = _fingerprint(Path(r["base_model"]))
        return [f"{k}: changed or missing" for k, v in before.items() if after.get(k) != v] + [f"{k}: new file in base model folder" for k in after if k not in before]

    def log_tail(self, rid: str, n: int = 40) -> str:
        r = self.db.get("training_runs", rid)
        p = Path(r["log_path"]) if r else None
        return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]) if p and p.is_file() else ""

    # ------------------------------------------------------------------ 6. decision, registry, activation, rollback
    def decide(self, rid: str, accept: bool, by: str = "user", note: str = "") -> dict:
        if by.lower() in ("agent", "model", "llm", "system"):
            raise PermissionError("Only a human can accept or reject a fine-tuned model.")
        r = self.db.get("training_runs", rid)
        if not r or r["status"] != "awaiting_decision":
            raise TrainingError("bad_state", "run is not awaiting a decision")
        base = Path(r["base_model"]).name
        mid = new_id("mdl_")
        adapter = Path(r["output_dir"]) / "adapter"
        self.db.insert("model_registry", {"id": mid, "kind": "adapter", "name": f"{base}-ft-{rid[-6:]}", "path": str(adapter), "base": r["base_model"], "run_id": rid,
                                          "status": "accepted" if accept else "rejected", "meta": {"metrics": r["metrics"].get("comparison"), "note": note}, "created": now()})
        self.db.update("training_runs", rid, {"status": "accepted" if accept else "rejected", "decision": f"{by}: {'accepted' if accept else 'rejected'} {note}", "updated": now()})
        self.db.audit(by, "training.decision", {"run": rid, "accepted": accept})
        return self.db.get("model_registry", mid)  # type: ignore[return-value]

    def registry(self) -> list[dict]:
        return self.db.query("SELECT * FROM model_registry ORDER BY created DESC")

    def activate(self, model_id: str, lmstudio_model_id: str, by: str = "user") -> dict:
        """Point NEXUS's chat model at the exported model *as named in LM Studio*; remember the previous one for rollback."""
        if by.lower() in ("agent", "model", "llm", "system"):
            raise PermissionError("Only a human can activate a model.")
        m = self.db.get("model_registry", model_id)
        if not m or m["status"] not in ("accepted", "rolled_back"):
            raise TrainingError("bad_state", "only an accepted adapter can be activated")
        prev = self.s.chat_model
        meta = m["meta"] or {}
        meta.update(previous_chat_model=prev, lmstudio_model_id=lmstudio_model_id)
        self.db.execute("UPDATE model_registry SET status='accepted' WHERE kind='adapter' AND status='active'")
        self.db.update("model_registry", model_id, {"status": "active", "meta": meta})
        save_overrides(self.s, {"chat_model": lmstudio_model_id})
        self.db.audit(by, "model.activate", {"id": model_id, "lmstudio": lmstudio_model_id, "previous": prev})
        return self.db.get("model_registry", model_id)  # type: ignore[return-value]

    def rollback(self, model_id: str, by: str = "user") -> dict:
        m = self.db.get("model_registry", model_id)
        if not m or m["status"] != "active":
            raise TrainingError("bad_state", "model is not active")
        prev = (m["meta"] or {}).get("previous_chat_model", "")
        save_overrides(self.s, {"chat_model": prev})
        self.db.update("model_registry", model_id, {"status": "rolled_back"})
        self.db.audit(by, "model.rollback", {"id": model_id, "restored": prev})
        return {"restored_chat_model": prev or "(automatic)", "adapter": m["name"], "base_model_files_touched": False}

    # ------------------------------------------------------------------ 7. export to LM Studio
    def export_plan(self, rid: str, quant: str = "Q4_K_M") -> dict:
        r = self.db.get("training_runs", rid)
        if not r:
            raise TrainingError("not_found", "no such run")
        model = self.check_model(r["base_model"])
        out = Path(r["output_dir"])
        lc = Path(self.s.llamacpp_dir).expanduser() if self.s.llamacpp_dir else None
        conv = (lc / "convert_hf_to_gguf.py") if lc else None
        quantizer = next((lc / n for n in ("llama-quantize", "llama-quantize.exe", "quantize", "build/bin/llama-quantize", "build/bin/Release/llama-quantize.exe") if lc and (lc / n).exists()), None)
        missing = []
        if not lc:
            missing.append("Set 'llama.cpp folder' in Settings (a clone/build of github.com/ggml-org/llama.cpp).")
        else:
            if not conv or not conv.exists():
                missing.append(f"convert_hf_to_gguf.py not found in {lc}")
            if not quantizer:
                missing.append(f"llama-quantize not found in {lc} (build llama.cpp or download a release)")
        if not model.get("gguf_convertible"):
            missing.append(f"model_type '{model.get('model_type')}' is not known to convert to GGUF - check llama.cpp's supported architectures")
        if not (out / "adapter").exists():
            missing.append("adapter folder does not exist yet (training has not produced one)")
        merged, f16, qf = out / "merged", out / "model-f16.gguf", out / f"model-{quant}.gguf"
        steps = [
            {"step": 1, "what": "Merge the LoRA adapter into a COPY of the base weights (original untouched)",
             "cmd": f'python -m nexus.training.merge --base "{r["base_model"]}" --adapter "{out / "adapter"}" --out "{merged}"'},
            {"step": 2, "what": "Convert merged Hugging Face weights to GGUF (f16)",
             "cmd": f'python "{conv}" "{merged}" --outfile "{f16}" --outtype f16' if conv else "python <llama.cpp>/convert_hf_to_gguf.py <merged> --outfile model-f16.gguf --outtype f16"},
            {"step": 3, "what": f"Quantize to {quant} so it fits consumer GPUs/RAM",
             "cmd": f'"{quantizer or "<llama.cpp>/llama-quantize"}" "{f16}" "{qf}" {quant}'},
            {"step": 4, "what": "Import into LM Studio", "cmd": f'lms import "{qf}"   (or copy it to LM Studio\'s models folder: <models>/<publisher>/<name>/)'},
            {"step": 5, "what": "In NEXUS: Models > Fine-tuning > Activate, entering the model id LM Studio shows. Compare with the base model on your own prompts first."}]
        return {"run": rid, "ready": not missing, "missing": missing, "steps": steps, "target_gguf": str(qf),
                "note": "A fine-tuned Transformers checkpoint is NOT directly loadable in LM Studio. It must be merged, converted to GGUF and (usually) quantized."}
