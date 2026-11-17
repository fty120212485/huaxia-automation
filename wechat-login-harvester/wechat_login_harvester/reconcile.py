from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import Config, UserCredential
from .oss import OssClient, OssDeleteResult
from .storage import make_storage, enabled as storage_enabled


@dataclass(frozen=True)
class ReconcileResult:
    to_harvest: list[UserCredential]
    skipped: list[UserCredential]
    deleted_local: list[str]
    oss_results: list[OssDeleteResult]


def local_account_path(config: Config, id_card: str) -> Path:
    return config.account_dir / f"{id_card}.account"


def _delete_local(config: Config, id_card: str) -> bool:
    path = config.account_dir / f"{id_card}.account"
    if path.exists():
        path.unlink()
        return True
    return False


def reconcile(config: Config) -> ReconcileResult:
    active: list[UserCredential] = []
    skipped: list[UserCredential] = []
    for user in config.users:
        if user.skip:
            skipped.append(user)
        else:
            active.append(user)

    deleted_local: list[str] = []
    for user in skipped:
        if _delete_local(config, user.id_card):
            deleted_local.append(user.id_card)

    oss_results: list[OssDeleteResult] = []
    if skipped and storage_enabled(config):
        try:
            oss = make_storage(config)
            for user in skipped:
                oss_results.append(
                    oss.delete_prefix(f"hxacc/account/{user.id_card}/")
                )
        except Exception as exc:
            oss_results.append(
                OssDeleteResult(
                    prefix=f"hxacc/account/*/",
                    deleted_count=0,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    to_harvest = [
        user
        for user in active
        if not (config.account_dir / f"{user.id_card}.account").exists()
    ]
    return ReconcileResult(
        to_harvest=to_harvest,
        skipped=skipped,
        deleted_local=deleted_local,
        oss_results=oss_results,
    )
