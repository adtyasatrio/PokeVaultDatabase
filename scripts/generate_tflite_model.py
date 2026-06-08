import tensorflow as tf
import os

def create_model():
    print("Loading EfficientNetB0 model...")
    # Load EfficientNetB0 without the top classification layer.
    # Pooling='avg' adds a GlobalAveragePooling2D layer,
    # converting the 2D feature map into a 1D vector (1280 elements).
    base_model = tf.keras.applications.EfficientNetB0(
        input_shape=(224, 224, 3),
        include_top=False,
        weights='imagenet',
        pooling='avg'
    )
    
    # We create a new model that takes an image and outputs the embedding
    inputs = tf.keras.Input(shape=(224, 224, 3))
    # EfficientNetB0 expects pixel values in [0, 255] and handles its own normalization.
    x = base_model(inputs)
    
    # We can L2 normalize the output embedding so Cosine Similarity is just a dot product.
    outputs = tf.keras.layers.UnitNormalization(axis=1)(x)

    model = tf.keras.Model(inputs, outputs)
    return model

def main():
    model = create_model()
    
    # Convert the model to TFLite
    print("Converting to TFLite...")
    converter = tf.lite.TFLiteConverter.from_keras_model(model)
    tflite_model = converter.convert()

    # Save the model
    os.makedirs('assets/models', exist_ok=True)
    model_path = 'assets/models/efficientnet_b0.tflite'
    with open(model_path, 'wb') as f:
        f.write(tflite_model)
    print(f"Model saved to {model_path} ({len(tflite_model) / 1024 / 1024:.2f} MB)")

if __name__ == '__main__':
    main()
