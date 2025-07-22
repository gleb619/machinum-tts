# app.py
import os
import shutil
import tempfile
import logging
import json
from flask import Flask, request, jsonify, send_file, render_template
from werkzeug.utils import secure_filename
from werkzeug.exceptions import HTTPException

# --- Local Imports ---
from config import Config, PRESETS
from services import FFmpegService, ID3TagService, TTSService, FileService
from errors import AppError
import time

# --- App Initialization & Logging ---
def create_app():
    """Application factory to create and configure the Flask app."""
    app = Flask(__name__)
    app.config.from_object(Config)

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    app.logger.setLevel(logging.INFO)

    return app

app = create_app()

# --- Service Instantiation ---
ffmpeg_service = FFmpegService()
id3_service = ID3TagService()
file_service = FileService(upload_folder=app.config['UPLOAD_FOLDER'])
tts_service = TTSService(server_url=app.config['TTS_SERVER_URL'])


# --- Error Handlers ---
@app.errorhandler(AppError)
def handle_app_error(error):
    """Handles custom application errors."""
    app.logger.error(f"Application Error: {error.description}")
    response = jsonify(error.to_dict())
    response.status_code = error.status_code
    return response

@app.errorhandler(413)
def handle_too_large(e):
    """Handles 'Request Entity Too Large' error."""
    max_mb = app.config["MAX_CONTENT_LENGTH"] // 1024 // 1024
    return jsonify({'error': f'File size exceeds the {max_mb}MB limit.'}), 413

@app.errorhandler(HTTPException)
def handle_http_exception(e):
    """Handles generic HTTP exceptions."""
    response = e.get_response()
    response.data = json.dumps({
        "code": e.code,
        "name": e.name,
        "error": e.description,
    })
    response.content_type = "application/json"
    return response

# --- HTML & Static Routes ---
@app.route('/')
def index():
    """Renders the main page of the application."""
    return render_template('index.html')

# --- API Routes ---
@app.route('/api/presets', methods=['GET'])
def get_presets():
    """Returns the available quality presets."""
    return jsonify(PRESETS)

@app.route('/health')
@app.route('/api/health')
def health_check():
    """Provides a health check for the service and its dependencies."""
    return jsonify({
        'status': 'healthy',
        'ffmpeg_available': ffmpeg_service.is_available(),
        'version': '2.0.0'
    })

@app.route('/api/enhance', methods=['POST'])
def enhance_mp3_route():
    """
    Endpoint to enhance one or more MP3 files.
    It applies audio filters based on a preset and adds ID3 tags.
    Returns a ZIP archive of the processed files.
    """
    ffmpeg_service.check_availability()
    files = request.files.getlist('files')
    if not files or not any(f.filename for f in files):
        raise AppError('No files were provided for enhancement.', 400)

    preset = request.form.get('preset', 'music')
    metadata = file_service.get_metadata_from_request(request)

    with tempfile.TemporaryDirectory() as temp_dir:
        processed_files = []
        for file in files:
            if not file or not file.filename:
                continue

            try:
                # Process each file in a sub-directory
                file_dir = os.path.join(temp_dir, secure_filename(os.path.splitext(file.filename)[0]))
                os.makedirs(file_dir)

                input_path = file_service.save_uploaded_file(file, file_dir)

                # Core processing steps
                extracted_path = ffmpeg_service.extract_audio_stream(input_path, file_dir)
                enhanced_path = ffmpeg_service.enhance_audio(extracted_path, file_dir, preset)
                id3_service.add_tags(enhanced_path, metadata)

                processed_files.append(enhanced_path)

            except Exception as e:
                app.logger.error(f"Failed to process file {file.filename}: {e}", exc_info=True)
                # Continue to next file even if one fails
                continue

        if not processed_files:
            raise AppError('None of the provided files could be processed.', 500)

        # Create a single zip file from all processed files
        zip_path = file_service.create_zip_archive(processed_files, temp_dir, "enhanced_audio")
        return send_file(zip_path, as_attachment=True, mimetype='application/zip')

@app.route('/api/join', methods=['POST'])
def join_mp3_route():
    """
    Joins multiple MP3 files into a single file.
    Files can be uploaded directly or as a single ZIP archive.
    Applies ID3 tags from the request or the first file.
    """
    ffmpeg_service.check_availability()
    output_filename = secure_filename(request.form.get('output_name', 'joined_audio.mp3'))
    should_enhance = str(request.form.get("enhance", "false")).lower() == "true"
    if not output_filename.lower().endswith('.mp3'):
        output_filename += '.mp3'

    with tempfile.TemporaryDirectory() as temp_dir:
        # Unpack uploaded files (either multi-part or a single zip)
        input_files = file_service.unpack_files_from_request(request, temp_dir)
        if len(input_files) < 2:
            raise AppError('At least two MP3 files are required for joining.', 400)

        # Join the files
        final_path = ffmpeg_service.join_files(input_files, temp_dir, output_filename)

        if should_enhance:
            preset = request.form.get("enhance_preset", "music")
            enhanced_path = ffmpeg_service.enhance_audio(final_path, temp_dir, preset, "enhanced_" + output_filename)

            # Determine and apply ID3 tags
            final_metadata = id3_service.extract_tags(input_files[0]) # Start with tags from the first file
            user_metadata = file_service.get_metadata_from_request(request)
            final_metadata.update(user_metadata) # Overwrite with user-provided tags

            id3_service.add_tags(enhanced_path, final_metadata)
            final_path = enhanced_path

        return send_file(final_path, as_attachment=True, mimetype='audio/mpeg')

@app.route('/api/tts', methods=['POST'])
def tts_route():
    """
    Generates audio from text using a TTS service.
    Optionally enhances the audio and adds metadata.
    Can return a single MP3 or a ZIP with the MP3 and a metadata file.
    """
    start_time = time.time()
    try:
        # Parse data from JSON or form
        app.logger.info("Got request to generate TTS, parsing incoming data.")
        data = request.get_json() if request.is_json else request.form.to_dict()
        text = data.get("text")
        if not text:
            raise AppError("Text is required for TTS generation.", 400)

        voice = data.get("voice", "ru-RU-DmitryNeural")
        output_filename = data.get("output_file", "tts_output.mp3")
        should_enhance = str(data.get("enhance", "false")).lower() == "true"
        return_zip = str(data.get("return_zip", "false")).lower() == "true"

        app.logger.info(f"Received text: {text[:50]}({len(text)}), voice: {voice}, output_file: {output_filename}, enhance: {should_enhance}, return_zip: {return_zip}")

        with tempfile.TemporaryDirectory() as temp_dir:
            # Generate the base TTS audio file
            app.logger.info("Starting TTS audio generation.")
            final_path = tts_service.generate_audio(text, voice, output_filename, temp_dir)

            # Optionally enhance the generated audio
            if should_enhance:
                ffmpeg_service.check_availability()
                preset = data.get("enhance_preset", "podcast")
                metadata = file_service.get_metadata_from_request(request)

                app.logger.info(f"Starting audio enhancement with preset: {preset}.")
                extracted_path = ffmpeg_service.extract_audio_stream(final_path, temp_dir, "extracted_" + output_filename)
                enhanced_path = ffmpeg_service.enhance_audio(extracted_path, temp_dir, preset, "enhanced_" + output_filename)
                id3_service.add_tags(enhanced_path, metadata)
                final_path = enhanced_path
                app.logger.info(f"Audio enhancement completed and saved to {final_path}.")

            # Prepare and send the response
            if return_zip:
                file_metadata = file_service.get_file_metadata(final_path)
                zip_path = file_service.create_zip_with_metadata(final_path, file_metadata, temp_dir)
                app.logger.info(f"Creating ZIP with metadata at {zip_path}.")
                return send_file(zip_path, as_attachment=True, mimetype='application/zip')
            else:
                app.logger.info(f"Sending audio file: {final_path} as attachment.")
                return send_file(final_path, as_attachment=True, download_name=output_filename, mimetype='audio/mpeg')

    except Exception as e:
        app.logger.error(f"TTS process failed: {e}", exc_info=True)
        # Convert any unexpected error to our standard error response
        if not isinstance(e, AppError):
            raise AppError("An internal error occurred during TTS processing.", 500)
        raise e

    finally:
        end_time = time.time()
        operation_duration = end_time - start_time
        app.logger.info(f"TTS operation took {operation_duration:.2f} seconds.")

# --- Main Execution ---
if __name__ == '__main__':
    is_debug = os.getenv('FLASK_DEBUG', 'False').lower() in ['true', '1']
    app.run(debug=is_debug, host='0.0.0.0', port=5003)