from flask import Flask, render_template, request, jsonify, send_file, after_this_request
import os
from datetime import datetime
import logging
import asyncio
import edge_tts
import time

# --- Basic Flask App Setup ---
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB max total payload size

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

async def generate_audio(text: str, voice: str, output_file: str) -> None:
    """
    Generates an audio file from the provided text using the specified voice.

    Args:
        text (str): The text to be converted into speech.
        voice (str): The voice model to use for generating the speech.
        output_file (str): The path where the generated audio file will be saved.

    Raises:
        Exception: If there is an issue during the audio generation process.
    """
    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(output_file)
        app.logger.info(f"Audio successfully generated and saved to {output_file}")
    except Exception as e:
        app.logger.error(f"Error during audio generation: {str(e)}")
        raise

@app.route('/health', methods=['GET'])
@app.route('/api/health', methods=['GET'])
def health_check():
    """Endpoint to check if the service is running."""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route("/api/tts", methods=["POST"])
def tts():
    """
    Endpoint to generate text-to-speech audio files.

    Expects a JSON payload with the following optional fields:
        - "text": The text to convert into speech (default: "Привет Мир!").
        - "voice": The voice model to use (default: "ru-RU-DmitryNeural").
        - "outputFile": The name of the output file (default: "output.mp3").

    Returns:
        Response: The generated audio file as an attachment or an error message.
    """
    start_time = time.time()
    try:
        # Parse request data
        data = request.json if request.is_json else {}
        text = data.get("text", "Привет Мир!")
        voice = data.get("voice", "ru-RU-DmitryNeural")
        output_file = data.get("outputFile", "output.mp3")

        # Generate audio file
        asyncio.run(generate_audio(text, voice, output_file))

        # Send the generated file as a response
        return send_file(
            output_file,
            mimetype="audio/mpeg",
            as_attachment=True,
            download_name=output_file
        )

    except Exception as e:
        app.logger.error(f"An error occurred in /tts endpoint: {str(e)}")
        return jsonify({"error": "An internal server error occurred."}), 500

    finally:
        # Clean up the generated file after sending it
        try:
            if os.path.exists(output_file):
                os.remove(output_file)
                app.logger.info(f"Temporary file {output_file} has been deleted.")
        except Exception as cleanup_error:
            app.logger.error(f"Failed to delete temporary file: {str(cleanup_error)}")

        end_time = time.time()
        operation_duration = end_time - start_time
        app.logger.info(f"TTS operation took {operation_duration:.2f} seconds.")

@app.route('/')
def index():
    """Render the main page of the application."""
    return render_template('index.html')

if __name__ == '__main__':
    # Use a production-ready WSGI server like Gunicorn or Waitress instead of app.run in production
    app.run(host='0.0.0.0', port=5001, debug=False)