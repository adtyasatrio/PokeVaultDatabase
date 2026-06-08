#!/usr/bin/env python3
import argparse
import os
import sqlite3
import struct
from typing import Optional


SOURCE_COLUMNS = (
    "id",
    "name",
    "set_name",
    "card_number",
    "hp",
    "set_id",
    "set_code",
    "image_url",
    "updated_at",
    "rarity",
    "types",
    "tflite_emb",
)


def quantize_embedding(blob: bytes) -> Optional[bytes]:
    if blob is None or len(blob) < 1280 * 4:
        return None

    values = struct.unpack("<1280f", blob[: 1280 * 4])
    return bytes(
        max(0, min(255, int(round((value + 1.0) * 127.5))))
        for value in values
    )


def create_schema(db: sqlite3.Connection) -> None:
    db.executescript(
        """
        PRAGMA journal_mode = OFF;
        PRAGMA synchronous = OFF;

        CREATE TABLE cards (
          id TEXT PRIMARY KEY,
          name TEXT NOT NULL,
          set_name TEXT,
          card_number TEXT,
          hp TEXT,
          set_id TEXT,
          set_code TEXT,
          image_url TEXT,
          updated_at TEXT,
          rarity TEXT,
          types TEXT,
          tflite_emb_q BLOB
        );

        CREATE INDEX idx_cards_name ON cards(name);
        CREATE INDEX idx_cards_set_number ON cards(set_id, card_number);
        """
    )


def compact(source_path: str, output_path: str, batch_size: int) -> None:
    if os.path.exists(output_path):
        os.remove(output_path)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    source = sqlite3.connect(source_path)
    target = sqlite3.connect(output_path)
    create_schema(target)

    select_sql = f"SELECT {', '.join(SOURCE_COLUMNS)} FROM cards"
    insert_sql = """
        INSERT INTO cards (
          id, name, set_name, card_number, hp, set_id, set_code,
          image_url, updated_at, rarity, types, tflite_emb_q
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """

    total = 0
    with target:
        for row in source.execute(select_sql):
            values = list(row)
            values[-1] = quantize_embedding(values[-1])
            target.execute(insert_sql, values)
            total += 1
            if total % batch_size == 0:
                target.commit()
                print(f"processed={total}")

    target.execute("VACUUM")
    integrity = target.execute("PRAGMA integrity_check").fetchone()[0]
    source.close()
    target.close()

    if integrity != "ok":
        raise RuntimeError(f"compact database failed integrity check: {integrity}")

    print(f"wrote={output_path}")
    print(f"cards={total}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Create a compact offline DB with uint8 TFLite embeddings."
    )
    parser.add_argument("--source", default="assets/db/poc.db")
    parser.add_argument("--output", default="build/offline_db/poc_compact.db")
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()
    compact(args.source, args.output, args.batch_size)


if __name__ == "__main__":
    main()
