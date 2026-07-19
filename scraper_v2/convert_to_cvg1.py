import json
import struct
import numpy as np

def main():
    # 1. Read card IDs
    with open('pokemon-cards-db-card-ids.json', 'r') as f:
        card_ids = json.load(f)

    count = len(card_ids)

    # 2. Read embeddings
    embeddings_f16 = np.fromfile('pokemon-cards-db-embeddings.f16.bin', dtype=np.float16)
    dimension = len(embeddings_f16) // count

    # The mobile app expects float32
    embeddings_f32 = embeddings_f16.astype(np.float32)

    # 3. Create cvg1_id_map.json (mapping integer index to string ID)
    id_map = {str(i): card_id for i, card_id in enumerate(card_ids)}
    with open('cvg1_id_map.json', 'w') as f:
        json.dump(id_map, f)

    # 4. Write tcgplayer_pokemon_catalog.bin in CVG1 format
    with open('tcgplayer_pokemon_catalog.bin', 'wb') as f:
        # Magic bytes (4 bytes)
        f.write(b'CVG1')
        
        # Count and dimension (int32, little endian)
        f.write(struct.pack('<ii', count, dimension))
        
        # productIds (int64, little endian)
        # Generate sequential IDs from 0 to count-1
        for i in range(count):
            f.write(struct.pack('<q', i))
            
        # embeddings (float32, little endian)
        f.write(embeddings_f32.tobytes())

    print(f"Successfully converted {count} items with dimension {dimension} to CVG1 format.")

if __name__ == '__main__':
    main()
