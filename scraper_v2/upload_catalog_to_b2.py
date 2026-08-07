#!/usr/bin/env python3
import sys
from pathlib import Path
from migrate_tcgcollector_images_to_b2 import B2NativeClient, ConfigValues, load_config, ImageTask

def main():
    if len(sys.argv) != 3:
        print("Usage: upload_catalog_to_b2.py <local_path> <b2_object_path>")
        sys.exit(1)
        
    local_path = sys.argv[1]
    b2_object_path = sys.argv[2]
    
    print(f"Loading {local_path}...")
    with open(local_path, "rb") as f:
        data = f.read()

    print("Authenticating with B2...")
    config = load_config()
    client = B2NativeClient(config)
    
    # We use ImageTask just to satisfy the method signature of B2NativeClient
    task = ImageTask(
        card_id="catalog",
        variant="catalog",
        source_url="",
        object_path=b2_object_path,
        db_column=""
    )
    
    print(f"Uploading to B2 as {b2_object_path} (size: {len(data)} bytes)...")
    client.upload_object(task, data, "application/zip")
    print("✅ B2 Upload Complete!")

if __name__ == "__main__":
    main()
