"""Configuration dataclasses for the LLM proxy."""

import re
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Provider:
    identifier: str
    upstream_url: str
    models: dict[str, str]
    model_patterns: list[tuple[re.Pattern[str], str]] = field(default_factory=list)


@dataclass(frozen=True)
class McpConfig:
    upstream_url: str
    api_key: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "McpConfig | None":
        if not raw:
            return None
        upstream_url = str(raw.get("upstream_url", "")).rstrip("/")
        api_key = str(raw.get("api_key", ""))
        if not upstream_url or not api_key:
            return None
        return cls(upstream_url=upstream_url, api_key=api_key)


@dataclass(frozen=True)
class ImageConfig:
    upstream_url: str
    api_key: str
    allowed_models: set[str] = field(default_factory=set)
    db_path: str = "image_usage.db"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "ImageConfig | None":
        if not raw:
            return None
        upstream_url = str(raw.get("upstream_url", "")).rstrip("/")
        api_key = str(raw.get("api_key", ""))
        if not upstream_url or not api_key:
            return None
        allowed_models = set(raw.get("allowed_models", []))
        db_path = str(raw.get("db_path", "image_usage.db"))
        return cls(
            upstream_url=upstream_url,
            api_key=api_key,
            allowed_models=allowed_models,
            db_path=db_path,
        )


@dataclass(frozen=True)
class LlmProxyConfig:
    providers: dict[str, Provider]
    mcp: McpConfig | None = None
    image: ImageConfig | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "LlmProxyConfig":
        if not raw:
            raise ValueError("configuration is empty")

        providers_cfg = raw.get("providers")
        if not providers_cfg:
            raise ValueError("missing required 'providers' configuration")
        if not isinstance(providers_cfg, list):
            raise ValueError("'providers' must be a list")

        providers: dict[str, Provider] = {}
        seen_identifiers: set[str] = set()

        for entry in providers_cfg:
            if not isinstance(entry, dict):
                raise ValueError("each provider entry must be a mapping")

            identifier = entry.get("identifier")
            upstream_url = str(entry.get("upstream_url", "")).rstrip("/")
            models = entry.get("models", {})

            if not isinstance(models, dict):
                raise ValueError(
                    f"invalid 'models' for provider {identifier!r}: expected a mapping"
                )
            if not identifier or not upstream_url:
                raise ValueError(
                    f"invalid provider entry: identifier={identifier!r} upstream_url={upstream_url!r}"
                )
            if identifier in seen_identifiers:
                raise ValueError(f"duplicate provider identifier: {identifier!r}")

            model_patterns: list[tuple[re.Pattern[str], str]] = []
            for pattern_str, target in models.items():
                try:
                    compiled = re.compile(f"^(?:{pattern_str})$")
                except re.error as exc:
                    raise ValueError(
                        f"invalid regex pattern for provider {identifier!r} "
                        f"model {pattern_str!r}: {exc}"
                    ) from exc
                model_patterns.append((compiled, target))

            seen_identifiers.add(identifier)
            providers[identifier] = Provider(
                identifier=identifier,
                upstream_url=upstream_url,
                models=models,
                model_patterns=model_patterns,
            )

        return cls(
            providers=providers,
            mcp=McpConfig.from_dict(raw.get("mcp")),
            image=ImageConfig.from_dict(raw.get("image")),
        )
