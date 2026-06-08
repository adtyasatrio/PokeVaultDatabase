import sqlite3
import numpy as np
import requests
import cv2
import os
import colorsys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import tensorflow as tf
except ImportError:
    print("Please install tensorflow.")
    exit(1)

DB_PATH = 'assets/db/poc.db'
MODEL_PATH = 'assets/models/efficientnet_b0.tflite'
MAX_WORKERS = 16  # Memproses 16 kartu secara bersamaan

# Thread-local storage untuk TFLite interpreter
# Ini penting karena setiap thread butuh interpreter sendiri agar aman (thread-safe)
thread_local = threading.local()

def get_interpreter():
    if not hasattr(thread_local, "interpreter"):
        # Supaya tidak ada warning LiteRT di terminal
        tf.get_logger().setLevel('ERROR')
        interpreter = tf.lite.Interpreter(model_path=MODEL_PATH)
        interpreter.allocate_tensors()
        thread_local.interpreter = interpreter
    return thread_local.interpreter

def reduce_saturation(img_rgb, factor=0.5):
    h, w = img_rgb.shape[:2]
    result = np.zeros_like(img_rgb, dtype=np.uint8)
    for y in range(h):
        for x in range(w):
            r, g, b = img_rgb[y, x].astype(float) / 255.0
            hue, l, s = colorsys.rgb_to_hls(r, g, b)
            s *= factor
            nr, ng, nb = colorsys.hls_to_rgb(hue, l, s)
            result[y, x] = [
                int(round(nr * 255)),
                int(round(ng * 255)),
                int(round(nb * 255)),
            ]
    return result

def reduce_saturation_fast(img_rgb, factor=0.5):
    hsv = cv2.cvtColor(img_rgb, cv2.COLOR_RGB2HSV).astype(np.float32)
    hsv[:, :, 1] *= factor
    hsv[:, :, 1] = np.clip(hsv[:, :, 1], 0, 255)
    hsv = hsv.astype(np.uint8)
    return cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB)

def histogram_stretch(img_rgb):
    result = img_rgb.copy().astype(np.float32)
    for c in range(3):
        channel = img_rgb[:, :, c].astype(np.float32)
        lo = np.percentile(channel, 2)
        hi = np.percentile(channel, 98)
        if hi <= lo:
            continue
        stretched = (channel - lo) * 255.0 / (hi - lo)
        result[:, :, c] = np.clip(stretched, 0, 255)
    return result.astype(np.uint8)

def preprocess_image(image_bytes):
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return None
    
    img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    img = reduce_saturation_fast(img, 0.5)
    img = histogram_stretch(img)
    img = cv2.GaussianBlur(img, (7, 7), 0)
    
    h, w = img.shape[:2]
    expected_w, expected_h = 224, 224
    scale = min(expected_w / w, expected_h / h)
    new_w, new_h = int(w * scale), int(h * scale)
    
    resized = cv2.resize(img, (new_w, new_h))
    canvas = np.zeros((expected_h, expected_w, 3), dtype=np.uint8)
    
    y_offset = (expected_h - new_h) // 2
    x_offset = (expected_w - new_w) // 2
    canvas[y_offset:y_offset+new_h, x_offset:x_offset+new_w] = resized
    img = canvas
    
    img = img.astype(np.float32)
    img = np.expand_dims(img, axis=0)
    return img

def augment_image(image_bytes):
    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return []
    
    augmented = []
    augmented.append(image_bytes)
    
    h, w = img.shape[:2]
    center = (w // 2, h // 2)
    
    M_pos = cv2.getRotationMatrix2D(center, 5, 1.0)
    rotated_pos = cv2.warpAffine(img, M_pos, (w, h), borderMode=cv2.BORDER_REFLECT)
    _, buf = cv2.imencode('.jpg', rotated_pos)
    augmented.append(buf.tobytes())
    
    M_neg = cv2.getRotationMatrix2D(center, -5, 1.0)
    rotated_neg = cv2.warpAffine(img, M_neg, (w, h), borderMode=cv2.BORDER_REFLECT)
    _, buf = cv2.imencode('.jpg', rotated_neg)
    augmented.append(buf.tobytes())
    
    bright = cv2.convertScaleAbs(img, alpha=1.0, beta=30)
    _, buf = cv2.imencode('.jpg', bright)
    augmented.append(buf.tobytes())
    
    dark = cv2.convertScaleAbs(img, alpha=1.0, beta=-30)
    _, buf = cv2.imencode('.jpg', dark)
    augmented.append(buf.tobytes())
    
    crop_margin_x = int(w * 0.05)
    crop_margin_y = int(h * 0.05)
    cropped = img[crop_margin_y:h-crop_margin_y, crop_margin_x:w-crop_margin_x]
    _, buf = cv2.imencode('.jpg', cropped)
    augmented.append(buf.tobytes())
    
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hsv[:, :, 0] = (hsv[:, :, 0].astype(int) + 8) % 180
    hue_shift = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
    _, buf = cv2.imencode('.jpg', hue_shift)
    augmented.append(buf.tobytes())
    
    hsv2 = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    hsv2[:, :, 0] = (hsv2[:, :, 0].astype(int) - 8) % 180
    hue_shift2 = cv2.cvtColor(hsv2, cv2.COLOR_HSV2BGR)
    _, buf = cv2.imencode('.jpg', hue_shift2)
    augmented.append(buf.tobytes())

    hsv3 = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    hsv3[:, :, 1] = np.clip(hsv3[:, :, 1] * 1.4, 0, 255)
    vivid = cv2.cvtColor(hsv3.astype(np.uint8), cv2.COLOR_HSV2BGR)
    _, buf = cv2.imencode('.jpg', vivid)
    augmented.append(buf.tobytes())
    
    contrast = cv2.convertScaleAbs(img, alpha=1.2, beta=0)
    _, buf = cv2.imencode('.jpg', contrast)
    augmented.append(buf.tobytes())
    
    return augmented

def l2_normalize(embedding):
    norm = np.linalg.norm(embedding)
    if norm == 0:
        return embedding
    return embedding / norm

def process_card(card_id, image_url):
    """
    Fungsi ini berjalan secara independen di setiap thread.
    Mendownload, augmentasi, inference, dan return hasil.
    """
    try:
        resp = requests.get(image_url, timeout=15)
        if resp.status_code != 200:
            return card_id, False, f"HTTP {resp.status_code}"
            
        raw_bytes = resp.content
        aug_bytes_list = augment_image(raw_bytes)
        if not aug_bytes_list:
            return card_id, False, "Failed to augment image."
            
        interpreter = get_interpreter()
        input_details = interpreter.get_input_details()
        output_details = interpreter.get_output_details()
        
        embeddings = []
        for aug_bytes in aug_bytes_list:
            input_data = preprocess_image(aug_bytes)
            if input_data is None:
                continue
                
            interpreter.set_tensor(input_details[0]['index'], input_data)
            interpreter.invoke()
            output_data = interpreter.get_tensor(output_details[0]['index'])
            embeddings.append(output_data[0])
            
        if not embeddings:
            return card_id, False, "No valid embeddings from augmentations."
            
        avg_embedding = np.mean(embeddings, axis=0)
        normalized_embedding = l2_normalize(avg_embedding).astype(np.float32)
        emb_bytes = normalized_embedding.tobytes()
        
        return card_id, True, emb_bytes
        
    except Exception as e:
        return card_id, False, str(e)

def main():
    if not os.path.exists(MODEL_PATH):
        print(f"Model not found at {MODEL_PATH}. Please run generate_tflite_model.py first.")
        return

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    try:
        cursor.execute("ALTER TABLE cards ADD COLUMN tflite_emb BLOB")
        conn.commit()
    except sqlite3.OperationalError:
        pass 

    cursor.execute("SELECT id, image_url FROM cards WHERE tflite_emb IS NULL")
    rows = cursor.fetchall()

    if not rows:
        print("No cards need TFLite embeddings processing.")
        conn.close()
        return

    total = len(rows)
    print(f"🚀 Memulai multi-threaded TFLite embedding generation untuk {total} kartu!")
    print(f"⚙️ Menggunakan {MAX_WORKERS} concurrent workers.")
    
    completed = 0
    success_count = 0
    fail_count = 0
    
    # Kunci (lock) untuk memastikan proses write ke SQLite aman antar thread
    db_lock = threading.Lock()

    # Eksekusi paralel
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(process_card, r[0], r[1]): r[0] for r in rows}
        
        for future in as_completed(futures):
            card_id, success, data = future.result()
            completed += 1
            
            if success:
                success_count += 1
                with db_lock:
                    cursor.execute("UPDATE cards SET tflite_emb = ? WHERE id = ?", (data, card_id))
                    # Commit per batch (opsional, tapi di sini kita commit per baris agar aman)
                    conn.commit()
                print(f"[{completed}/{total}] ✅ {card_id} -> Success")
            else:
                fail_count += 1
                print(f"[{completed}/{total}] ❌ {card_id} -> Failed: {data}")

    conn.close()
    print("\n🎉 Selesai!")
    print(f"Berhasil: {success_count}, Gagal: {fail_count}")

if __name__ == '__main__':
    main()
