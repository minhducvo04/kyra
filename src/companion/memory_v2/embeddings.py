"""Local-only memory embeddings; shared benchmark text tower."""
import os
from functools import lru_cache


class TextTower:
    """EmbeddingGemma 2's text tower alone, for when sentence-transformers cannot build the multimodal processor
    (transformers 5.19 fails on EmbeddingGemma2Processor). Loads only the language_model.* weights, then the card's
    pooling: mean over the attention mask, then L2 normalise. Same `encode` shape as SentenceTransformer."""

    def __init__(self, model_id: str, bf16: bool = False):
        import torch
        from huggingface_hub import hf_hub_download
        from safetensors import safe_open
        from transformers import AutoConfig, AutoTokenizer
        from transformers.models.embedding_gemma2.modeling_embedding_gemma2 import EmbeddingGemma2TextModel
        self.torch = torch
        self.device = "mps" if torch.backends.mps.is_available() else "cpu"
        self.model = EmbeddingGemma2TextModel(AutoConfig.from_pretrained(model_id, local_files_only=True).text_config)
        state = {}
        with safe_open(hf_hub_download(model_id, "model.safetensors", local_files_only=True), "pt") as f:
            for k in f.keys():  # noqa: SIM118 (safe_open is not a dict)
                if k.startswith("language_model."):
                    state[k[len("language_model."):]] = f.get_tensor(k)
        missing, unexpected = self.model.load_state_dict(state, strict=False)
        if missing or unexpected:
            raise RuntimeError(f"text tower weights do not match: {len(missing)} missing, {len(unexpected)} unexpected")
        self.model = self.model.to(self.device, torch.bfloat16 if bf16 else torch.float32).eval()
        self.tok = AutoTokenizer.from_pretrained(model_id, local_files_only=True)

    def encode(self, texts: list, normalize_embeddings=True, batch_size=32):
        torch, out = self.torch, []
        with torch.no_grad():
            order = sorted(range(len(texts)), key=lambda i: len(texts[i]))  # similar lengths per batch: less padding
            for i in range(0, len(order), batch_size):
                b = self.tok([texts[j] for j in order[i:i + batch_size]], padding=True, truncation=True, max_length=512,
                             return_tensors="pt").to(self.device)
                h = self.model(**b).last_hidden_state
                m = b["attention_mask"].unsqueeze(-1).to(h.dtype)
                e = ((h * m).sum(1) / m.sum(1)).float()
                out.append(torch.nn.functional.normalize(e, dim=-1).cpu())
        vecs = torch.cat(out)
        return vecs[torch.argsort(torch.tensor(order))].numpy()  # back to input order


@lru_cache(maxsize=2)
def load_model(name):
    os.environ["HF_HUB_OFFLINE"] = "1"
    if name == "gemma2":
        return TextTower("google/embeddinggemma-2", bf16=True)
    if name == "bge-small":
        from sentence_transformers import SentenceTransformer
        return SentenceTransformer("BAAI/bge-small-en-v1.5", local_files_only=True)
    raise ValueError("unknown memory embedding model")


def encode(texts, name, *, query=False):
    import numpy as np
    if not texts:
        return np.empty((0, 768 if name == "gemma2" else 384), dtype="float32")
    prefix = ("task: search result | query: " if query else "title: none | text: ") if name == "gemma2" else ""
    return load_model(name).encode([prefix + t for t in texts], normalize_embeddings=True, batch_size=32)
