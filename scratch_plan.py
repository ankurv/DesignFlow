def _verify_model(self, model_id: str) -> bool:
    try:
        # Use a temporary config to test so we don't clobber the real one until we are sure
        # Wait, if we change self.config.model, _raw_send will use the new one.
        old_model = self.config.model
        self.config.model = model_id
        # Actually some providers might need reconfigure_client if model changes? No, OpenAIAgent uses self.config.model in _raw_send directly.
        self._raw_send([{"role": "user", "content": "ping"}], "Respond exactly with 'pong'.")
        self.config.model = old_model
        return True
    except Exception as exc:
        print(f"[{self.name}] JIT Verification failed for {model_id}: {exc}")
        self.config.model = old_model
        return False

def _ensure_working_model(self) -> bool:
    from .base import DEFAULT_MODELS
    from .providers import discover_models
    
    actual_model = self.config.model or DEFAULT_MODELS.get(self.config.kind, "")
    if actual_model and self._verify_model(actual_model):
        self.config.model = actual_model
        return True
        
    catalog = discover_models(self.config)
    for candidate in catalog:
        if candidate == actual_model:
            continue
        if self._verify_model(candidate):
            self.config.model = candidate
            return True
            
    return False
