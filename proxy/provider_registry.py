"""Built-in provider definitions shared by the proxy and Ultra orchestrator."""

PROVIDERS = {
    "deepseek": {
        "mode": "anthropic",
        "dsml_capable": True,
        "url": "https://api.deepseek.com/anthropic/v1/messages",
        "key_env": "DEEPSEEK_API_KEY",
        # Science only puts claude-{opus|sonnet|haiku}-<numeric version> ids in
        # its main model list. These shell ids keep both DeepSeek tiers visible;
        # model_map translates them back to the actual upstream ids.
        "models": [
            ("claude-opus-4-8", "DeepSeek V4 Pro"),
            ("claude-haiku-4-5", "DeepSeek V4 Flash"),
        ],
        "model_map": {
            "claude-opus-4-8": "deepseek-v4-pro",
            "claude-sonnet-5": "deepseek-v4-flash",
            "claude-sonnet-4-6": "deepseek-v4-flash",
            "claude-haiku-4-5": "deepseek-v4-flash",
        },
        "model_caps": {
            "deepseek-v4-pro": 65536,
            "deepseek-v4-flash": 32768,
        },
        "default_cap": 8192,
        "default_model": "deepseek-v4-flash",
    },
    "qwen": {
        "mode": "openai",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "key_env": "DASHSCOPE_API_KEY",
        "models": [
            ("qwen-max", "Qwen Max"),
            ("qwen-plus", "Qwen Plus"),
            ("qwen-turbo", "Qwen Turbo"),
        ],
        "model_map": {
            "claude-opus-4-8": "qwen-max",
            "claude-sonnet-5": "qwen-plus",
            "claude-sonnet-4-6": "qwen-plus",
            "claude-haiku-4-5": "qwen-turbo",
        },
        "model_caps": {
            "qwen-max": 8192,
            "qwen-plus": 8192,
            "qwen-turbo": 8192,
        },
        "default_cap": 8192,
        "default_model": "qwen-plus",
    },
    "openai-custom": {
        "mode": "openai",
        "api_format": "openai_chat",
        "url": None,
        "models_url": None,
        "key_env": "CSSWITCH_OPENAI_KEY",
        "auth_style": "bearer",
        "force_model_override": True,
        "models": [],
        "model_map": {},
        "model_caps": {},
        "default_cap": None,
        "default_model": "",
    },
    "openai-responses": {
        "mode": "openai",
        "api_format": "openai_responses",
        "url": None,
        "models_url": None,
        "key_env": "CSSWITCH_OPENAI_KEY",
        "auth_style": "bearer",
        "force_model_override": True,
        "models": [],
        "model_map": {},
        "model_caps": {},
        "default_cap": 65536,
        "default_model": "",
    },
    "relay": {
        "mode": "anthropic",
        "url": None,
        "models_url": None,
        "key_env": "CSSWITCH_RELAY_KEY",
        "passthrough": True,
        "force_model_override": True,
        "auth_style": "both",
        "models": [],
        "model_map": {},
        "model_caps": {},
        "default_cap": None,
        "default_model": "claude-opus-4-8",
    },
}
