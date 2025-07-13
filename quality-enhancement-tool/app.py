from flask import Flask, render_template, request, jsonify, send_file
import os
import tempfile
import subprocess
import shutil
import logging
import requests
import uuid
import zipfile
import json
from werkzeug.utils import secure_filename
from mutagen.id3 import (
    ID3, TIT2, TPE1, TALB, TDRC, APIC, TCON, TLAN, TRCK, TPUB,
    TCOP, COMM
)
from mutagen.mp3 import MP3


# --- App Initialization & Configuration ---
app = Flask(__name__)
app.config.update(
    MAX_CONTENT_LENGTH=50 * 1024 * 1024,  # 50MB max file size
    UPLOAD_FOLDER=tempfile.gettempdir(),
)
SERVER_URL = os.getenv("SERVER_URL", "http://localhost:5001/api/tts")

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constants ---
PRESETS = {
    'podcast': {
        'bitrate': '128k', 'sample_rate': '44100', 'channels': '1',
        'loudnorm': {'I': -16, 'LRA': 11, 'TP': -1.5},
        'description': 'Voice-optimized preset'
    },
    'music': {
        'bitrate': '256k', 'sample_rate': '44100', 'channels': '2',
        'loudnorm': {'I': -14, 'LRA': 7, 'TP': -1.0},
        'description': 'Wide dynamic range preset'
    },
    'high_quality': {
        'bitrate': '320k', 'sample_rate': '44100', 'channels': '2',
        'loudnorm': {'I': -16, 'LRA': 11, 'TP': -1.5},
        'description': 'Maximum quality preset'
    }
}

# --- Helper Functions ---
def _run_command(cmd, error_msg="FFmpeg command failed"):
    """Run a subprocess command, handling errors and logging."""
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True, encoding='utf-8')
        return True
    except subprocess.CalledProcessError as e:
        logger.error(f"{error_msg}: {e.stderr}")
        return False
    except FileNotFoundError:
        logger.error(f"Command not found: {cmd[0]}. Please ensure it's in the system PATH.")
        return False

def check_ffmpeg():
    """Check if FFmpeg is available in system PATH"""
    return _run_command(['ffmpeg', '-version'], "FFmpeg check failed")

# --- Core Audio Processing Functions ---
def extract_audio_stream(input_path, output_path):
    """Extract audio stream without metadata using FFmpeg"""
    cmd = ['ffmpeg', '-i', input_path, '-map', '0:a', '-c:a', 'libmp3lame', '-q:a', '0', '-y', output_path]
    logger.info(f"Extracting audio stream from {os.path.basename(input_path)}...")
    return _run_command(cmd, "FFmpeg extraction failed")

def enhance_audio_quality(input_path, output_path, preset='music'):
    """Apply quality enhancement using FFmpeg with loudnorm"""
    config = PRESETS.get(preset, PRESETS['music'])
    ln = config['loudnorm']
    loudnorm_filter = f"loudnorm=I={ln['I']}:LRA={ln['LRA']}:TP={ln['TP']}"
    cmd = [
        'ffmpeg', '-i', input_path, '-af', loudnorm_filter,
        '-c:a', 'libmp3lame', '-b:a', config['bitrate'], '-ar', config['sample_rate'],
        '-ac', config['channels'], '-y', output_path
    ]
    logger.info(f"Enhancing audio quality for {os.path.basename(input_path)} with preset '{preset}'...")
    return _run_command(cmd, "FFmpeg enhancement failed")

def add_id3_tags(file_path, metadata):
    """Add ID3 tags using Mutagen, including sanitizing unwanted tags."""
    try:
        audio = MP3(file_path, ID3=ID3)
        audio.delete()  # Clear existing tags before adding new ones

        tag_map = {
           'title': TIT2,
           'artist': TPE1,
           'album': TALB,
           'year': TDRC,
           'genre': TCON,
           'language': TLAN,
           'track': TRCK,
           'publisher': TPUB,
           'copyright': TCOP,
        }

        # Add standard tags
        for key, tag_class in tag_map.items():
            if metadata.get(key):
                audio[tag_class.__name__] = tag_class(encoding=3, text=metadata[key])

        # Handle comments
        if metadata.get('comments'):
            audio['COMM::eng'] = COMM(encoding=3, lang='eng', desc='', text=metadata['comments'])

        if metadata.get('cover_art'):
            audio['APIC'] = APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=metadata['cover_art'])

        audio.save()
        logger.info(f"ID3 tags added successfully to {os.path.basename(file_path)}")
        return True
    except Exception as e:
        logger.error(f"Failed to add ID3 tags: {e}")
        return False

def extract_id3_tags(file_path):
    """Extract ID3 tags from an MP3 file."""
    try:
        audio = MP3(file_path, ID3=ID3)
        tags = {}

        # Standard tag extraction
        tag_mappings = {
            'TIT2': 'title',
            'TPE1': 'artist',
            'TALB': 'album',
            'TDRC': 'year',
            'TCON': 'genre',
            'TLAN': 'language',
            'TRCK': 'track',
            'TPUB': 'publisher',
            'TCOP': 'copyright',
        }

        for tag_id, key in tag_mappings.items():
            if tag_id in audio:
                value = audio[tag_id].text[0] if audio[tag_id].text else ""
                tags[key] = str(value)

        # Extract comments
        for frame_id in audio:
            if frame_id.startswith('COMM'):
                tags['comments'] = audio[frame_id].text[0] if audio[frame_id].text else ""
                break

        # Extract cover art
        for frame_id in audio:
            if frame_id.startswith('APIC'):
                apic_frame = audio[frame_id]
                tags['cover_art'] = apic_frame.data
                tags['cover_mime'] = apic_frame.mime
                tags['cover_type'] = apic_frame.type
                tags['cover_desc'] = apic_frame.desc
                break

        return tags
    except Exception as e:
        logger.error(f"Failed to extract ID3 tags: {e}")
        return {}

def join_mp3_files(file_paths, output_path):
    """Join multiple MP3 files into one using FFmpeg's concat demuxer."""
    if len(file_paths) < 2:
        shutil.copy(file_paths[0], output_path)
        return True

    concat_file_path = output_path + '.txt'
    try:
        with open(concat_file_path, 'w', encoding='utf-8') as f:
            for path in file_paths:
                f.write(f"file '{os.path.abspath(path)}'\n")

        cmd = ['ffmpeg', '-f', 'concat', '-safe', '0', '-i', concat_file_path, '-c', 'copy', '-y', output_path]
        logger.info(f"Joining {len(file_paths)} files into {os.path.basename(output_path)}...")
        return _run_command(cmd, "FFmpeg join failed")
    finally:
        if os.path.exists(concat_file_path):
            os.remove(concat_file_path)

def generate_tts_audio(text, voice, tts_server_url, save_directory):
    """Makes a request to the TTS server and saves the returned audio file."""
    try:
        response = requests.post(tts_server_url, json={"text": text, "voice": voice}, stream=True, timeout=120)
        response.raise_for_status()
        output_path = os.path.join(save_directory, f"tts_{uuid.uuid4()}.mp3")
        with open(output_path, 'wb') as f:
            for chunk in response.iter_content(chunk_size=8192):
                f.write(chunk)
        logger.info(f"TTS audio saved successfully to {output_path}")
        return output_path
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to communicate with TTS server: {e}")
        return None

def collect_file_metadata(file_path, filename):
    """Collect comprehensive metadata about the audio file."""
    metadata = {
        'filename': filename,
        'file_size': 0,
        'duration_seconds': 0,
        'bitrate': 0,
        'sample_rate': 0,
        'channels': 0,
        'format': 'mp3'
    }

    try:
        # Get file size
        metadata['file_size'] = os.path.getsize(file_path)

        # Get audio metadata using mutagen
        audio = MP3(file_path)
        if audio.info:
            metadata['duration_seconds'] = round(audio.info.length, 2)
            metadata['bitrate'] = audio.info.bitrate
            metadata['sample_rate'] = audio.info.sample_rate
            metadata['channels'] = audio.info.channels

        # Extract ID3 tags
        id3_tags = extract_id3_tags(file_path)
        if id3_tags:
            metadata['id3_tags'] = id3_tags

    except Exception as e:
        logger.error(f"Failed to collect metadata: {e}")

    return metadata

def create_zip_response(file_path, metadata, filename, temp_dir):
    """Create a zip file containing the audio file and metadata JSON."""
    zip_filename = os.path.splitext(filename)[0] + '.zip'
    zip_path = os.path.join(temp_dir, zip_filename)

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            # Add audio file
            zipf.write(file_path, filename)

            # Add metadata JSON
            metadata_json = json.dumps(metadata, indent=2, default=str)
            metadata_filename = os.path.splitext(filename)[0] + '_metadata.json'
            zipf.writestr(metadata_filename, metadata_json)

        return send_file(zip_path, as_attachment=True, download_name=zip_filename, mimetype='application/zip')

    except Exception as e:
        logger.error(f"Failed to create zip file: {e}")
        raise RuntimeError("Failed to create zip archive")


# --- Flask Routes ---
@app.route('/')
def index():
    """Render the main page of the application."""
    return render_template('index.html')

@app.route('/api/presets', methods=['GET'])
def get_presets():
    """Get available quality presets"""
    return jsonify(PRESETS)

# Add tags Genre, Language, Track Number, Publisher, Copyright, Encoded By, Comments
@app.route('/api/enhance', methods=['POST'])
def enhance_mp3():
    """Main endpoint for enhancing multiple MP3s and returning them in a ZIP."""
    if not check_ffmpeg():
        return jsonify({'error': 'FFmpeg not found in system PATH'}), 500
    files = request.files.getlist('files')
    if not files or not any(f.filename for f in files):
        return jsonify({'error': 'No files selected for upload'}), 400

    preset = request.form.get('preset', 'music')
    if preset not in PRESETS:
        return jsonify({'error': f'Invalid preset: {preset}'}), 400

    # metadata = {
    #     'title': request.form.get('title'),
    #     'artist': request.form.get('artist'),
    #     'album': request.form.get('album'),
    #     'year': request.form.get('year')
    # }
    id3_tags = request.form.get("metadata", {})
    # Parse the JSON string into a Python dictionary
    try:
        metadata = json.loads(id3_tags)
    except json.JSONDecodeError:
        return jsonify({"error": "Invalid JSON in 'metadata' field"}), 400

    if 'cover_art' in request.files and request.files['cover_art'].filename:
        metadata['cover_art'] = request.files['cover_art'].read()

    with tempfile.TemporaryDirectory() as temp_dir:
        enhanced_dir = os.path.join(temp_dir, 'enhanced_files')
        os.makedirs(enhanced_dir)

        for file in files:
            if not file or not file.filename: continue

            try:
                input_filename = secure_filename(file.filename)
                input_path = os.path.join(temp_dir, input_filename)
                file.save(input_path)

                extracted_path = os.path.join(temp_dir, f"extracted_{input_filename}")
                if not extract_audio_stream(input_path, extracted_path):
                    logger.error(f"Skipping file {input_filename} due to extraction error.")
                    continue

                enhanced_path = os.path.join(enhanced_dir, f"enhanced_{input_filename}")
                if not enhance_audio_quality(extracted_path, enhanced_path, preset):
                    logger.error(f"Skipping file {input_filename} due to enhancement error.")
                    continue

                if not add_id3_tags(enhanced_path, {k: v for k, v in metadata.items() if v}):
                    logger.warning(f"Failed to add ID3 tags to {input_filename}, but continuing.")
            except Exception as e:
                logger.error(f"Failed to process file {file.filename}: {e}", exc_info=True)
                continue # Move to the next file

        if not os.listdir(enhanced_dir):
            return jsonify({'error': 'No files could be processed successfully'}), 500

        # Create a ZIP archive of the enhanced files
        zip_path = os.path.join(temp_dir, 'enhanced_audio')
        shutil.make_archive(zip_path, 'zip', enhanced_dir)

        return send_file(f"{zip_path}.zip", as_attachment=True, download_name='enhanced_audio.zip', mimetype='application/zip')

@app.route('/api/join', methods=['POST'])
def join_mp3_route():
    """Join multiple MP3 files, accepting either multiple files or a single ZIP."""
    if not check_ffmpeg():
        return jsonify({'error': 'FFmpeg not found in system PATH'}), 500

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_files = []
        # Case 1: A ZIP file is uploaded
        if 'file' in request.files and request.files['file'].filename.lower().endswith('.zip'):
            zip_file = request.files['file']
            zip_path = os.path.join(temp_dir, secure_filename(zip_file.filename))
            zip_file.save(zip_path)

            extract_dir = os.path.join(temp_dir, 'extracted_zip')
            os.makedirs(extract_dir)
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(extract_dir)

            for root, _, files in os.walk(extract_dir):
                for f in files:
                    if f.lower().endswith('.mp3'):
                        temp_files.append(os.path.join(root, f))
            temp_files.sort() # Sort to ensure a predictable join order

        # Case 2: Multiple MP3 files are uploaded
        else:
            files = request.files.getlist('files')
            if not files: return jsonify({'error': 'No files or ZIP archive uploaded'}), 400

            for i, file in enumerate(files):
                if file and file.filename.lower().endswith('.mp3'):
                    filename = secure_filename(file.filename)
                    temp_path = os.path.join(temp_dir, f"{i:03d}_{filename}")
                    file.save(temp_path)
                    temp_files.append(temp_path)

        if len(temp_files) < 2:
            return jsonify({'error': 'At least 2 valid MP3 files are required for joining'}), 400

        # Extract ID3 tags from the first file
        id3_tags = extract_id3_tags(temp_files[0]) if temp_files else {}

        # Override with user-provided ID3 tags if present
        # user_tags = {
        #     'title': request.form.get('title'),
        #     'artist': request.form.get('artist'),
        #     'album': request.form.get('album'),
        #     'year': request.form.get('year')
        # }
        metadata = request.form.get("metadata", {})
        # Parse the JSON string into a Python dictionary
        try:
            user_tags = json.loads(metadata)
        except json.JSONDecodeError:
            return jsonify({"error": "Invalid JSON in 'metadata' field"}), 400

        id3_tags.update({k: v for k, v in user_tags.items() if v})  # Update only non-empty values

        if 'cover_art' in request.files and request.files['cover_art'].filename:
            id3_tags['cover_art'] = request.files['cover_art'].read()

        output_filename = secure_filename(request.form.get('output_name', 'joined_audio.mp3'))
        if not output_filename.endswith('.mp3'): output_filename += '.mp3'
        output_path = os.path.join(temp_dir, output_filename)

        if not join_mp3_files(temp_files, output_path):
            return jsonify({'error': 'Failed to join MP3 files'}), 500

        # Apply extracted ID3 tags to the final file
        if id3_tags:
            add_id3_tags(output_path, id3_tags)

        return send_file(output_path, as_attachment=True, download_name=output_filename, mimetype='audio/mpeg')

@app.route('/api/tts', methods=['POST'])
def tts_endpoint():
    """Endpoint to generate TTS audio, with an option to enhance it."""
    temp_dir = None
    try:
        # Handle both JSON and form data
        if request.is_json:
            data = request.json
            cover_art_data = None
        else:
            data = request.form.to_dict()
            # Handle file upload for cover art
            cover_art_file = request.files.get('cover_art')
            cover_art_data = cover_art_file.read() if cover_art_file else None

        text = data.get("text")
        voice = data.get("voice", "ru-RU-DmitryNeural")
        if not text:
            return jsonify({'error': 'Text is required'}), 400

        output_filename = data.get("output_file", "tts_output.mp3")
        should_enhance = data.get("enhance", "false").lower() == "true"
        return_zip = data.get("return_zip", "false").lower() == "true"

        temp_dir = tempfile.mkdtemp()

        try:
            # Step 1: Generate initial TTS audio
            generated_path = generate_tts_audio(text, voice, SERVER_URL, temp_dir)
            if not generated_path:
                raise RuntimeError("Failed to generate base TTS audio.")

            final_path = generated_path

            # Step 2: Optionally enhance the generated audio
            if should_enhance:
                preset = data.get("enhance_preset", "podcast")

                # metadata = data.get("metadata", {})
                id3_tags = data.get("metadata", {})
                # Parse the JSON string into a Python dictionary
                try:
                    metadata = json.loads(id3_tags)
                except json.JSONDecodeError:
                    raise RuntimeError("Invalid JSON in 'metadata' field")

                if cover_art_data:
                    metadata['cover_art'] = cover_art_data

                extracted_path = os.path.join(temp_dir, 'extracted.mp3')
                if not extract_audio_stream(generated_path, extracted_path):
                    raise RuntimeError("Failed to extract stream from TTS audio for enhancement.")

                enhanced_path = os.path.join(temp_dir, 'enhanced.mp3')
                if not enhance_audio_quality(extracted_path, enhanced_path, preset):
                    raise RuntimeError("Failed to enhance TTS audio.")

                if metadata:
                    add_id3_tags(enhanced_path, metadata)

                final_path = enhanced_path

            # Step 3: Return file with metadata or zip
            if return_zip:
                # Step 4: Collect file metadata
                file_metadata = collect_file_metadata(final_path, output_filename)
                return create_zip_response(final_path, file_metadata, output_filename, temp_dir)
            else:
                return send_file(final_path, as_attachment=True, download_name=output_filename, mimetype='audio/mpeg')

        except Exception as e:
            logger.error(f"TTS process failed: {e}", exc_info=True)
            return jsonify({'error': str(e)}), 500

    except Exception as e:
        logger.error(f"Request parsing failed: {e}", exc_info=True)
        return jsonify({'error': 'Invalid request format'}), 400

    finally:
        # Clean up temp files
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir)
            except Exception as e:
                logger.warning(f"Failed to cleanup temp directory {temp_dir}: {e}")

@app.route('/api/health')
@app.route('/health')
def health_check():
    """Health check endpoint to verify service status and dependencies."""
    return jsonify({
        'status': 'healthy',
        'ffmpeg_available': check_ffmpeg(),
        'version': '1.2.0'
    })

@app.errorhandler(413)
def too_large(e):
    """Custom error handler for 'Request Entity Too Large'."""
    max_mb = app.config["MAX_CONTENT_LENGTH"] // 1024 // 1024
    return jsonify({'error': f'File too large (max {max_mb}MB)'}), 413

if __name__ == '__main__':
    app.run(debug=os.getenv('FLASK_DEBUG', 'False').lower() in ['true', '1'], host='0.0.0.0', port=5003)