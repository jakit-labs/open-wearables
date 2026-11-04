#!/usr/bin/env python3
"""Seed the API key BH.Loop's API sends, from SEED_API_KEY (BH.Loop fork, T-CYK-1009 S20).

The operator mints the key straight into Secret Manager through a pipe, and Cloud Run
hands it to the migrate step as SEED_API_KEY. Only its hash and display prefix are
stored, as for a minted key. Nothing here prints the key.
"""

import re
import sys

from app.config import settings
from app.database import SessionLocal
from app.schemas.model_crud.credentials import ApiKeyCreate
from app.services.api_key_service import KEY_PREFIX_LENGTH, api_key_service
from app.utils.security import hash_api_key

# The shape ApiKeyService mints: "sk-" and 32 lowercase hex characters.
KEY_SHAPE = re.compile(r"sk-[0-9a-f]{32}")


def seed_api_key(raw_key: str) -> None:
    """Store the seeded key's hash once; a second start finds it and adds nothing."""
    if not KEY_SHAPE.fullmatch(raw_key):
        sys.exit("SEED_API_KEY_REFUSED: the key is not sk- and 32 lowercase hex characters.")
    key_hash = hash_api_key(raw_key)
    with SessionLocal() as db:
        if api_key_service.crud.get_by_hash(db, key_hash):
            print("The seeded API key is already stored, skipping.")
            return
        creator = ApiKeyCreate(key_hash=key_hash, key_prefix=raw_key[:KEY_PREFIX_LENGTH], name="BH.Loop API")
        api_key_service.crud.create(db, creator)
        print("✓ Seeded the API key from SEED_API_KEY.")


def main() -> None:
    if settings.seed_api_key is None:
        print("No SEED_API_KEY, skipping the API key seed.")
        return
    seed_api_key(settings.seed_api_key.get_secret_value())


if __name__ == "__main__":
    main()
