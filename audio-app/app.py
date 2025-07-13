from flask import Flask, render_template, request, jsonify, send_file, after_this_request
import os
import tempfile
import numpy as np
import librosa
import noisereduce as nr
from pydub import AudioSegment
import pyloudnorm as pyln
from werkzeug.utils import secure_filename
import uuid
from datetime import datetime
import logging
import traceback
import requests
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import zipfile
import shutil

# --- Basic Flask App Setup ---
app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 200 * 1024 * 1024  # 200MB max total payload size

# --- Logging Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Constants and Configuration ---
# Load configuration values from environment variables or use defaults
ALLOWED_EXTENSIONS = set(os.getenv("ALLOWED_EXTENSIONS", "wav").split(","))
TTS_SERVICE_URL = os.getenv("TTS_SERVICE_URL", "http://localhost:5002/api/tts")
MAX_CHUNK_LENGTH = int(os.getenv("MAX_CHUNK_LENGTH", 180))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))
RETRY_DELAY = int(os.getenv("RETRY_DELAY", 1))  # seconds

def allowed_file(filename):
    """Checks if the uploaded file has an allowed extension."""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def extract_zip(file_stream):
    """Extracts files from a zip archive and returns valid files."""
    extracted_files = []
    with tempfile.TemporaryDirectory() as temp_dir:
        with zipfile.ZipFile(file_stream, 'r') as zip_ref:
            zip_ref.extractall(temp_dir)
        for root, _, files in os.walk(temp_dir):
            for filename in files:
                if allowed_file(filename):
                    file_path = os.path.join(root, filename)
                    with open(file_path, 'rb') as f:
                        extracted_files.append(f)
    return extracted_files

class TextChunker:
    """Handles text splitting into TTS-appropriate chunks."""

    def split_text(self, text, max_length=MAX_CHUNK_LENGTH):
        if not text.strip():
            return []

        # First split by lines
        lines = text.strip().split('\n')
        chunks = []
        current_chunk = ""

        for line in lines:
            line = line.strip()
            if not line:
                continue

            # If adding this line would exceed max_length, process current chunk
            if current_chunk and len(current_chunk) + len(line) + 1 > max_length:
                chunks.extend(self._split_chunk(current_chunk, max_length))
                current_chunk = line
            else:
                current_chunk = current_chunk + ('\n' if current_chunk else '') + line

        # Process remaining chunk
        if current_chunk:
            chunks.extend(self._split_chunk(current_chunk, max_length))

        return chunks

    def _split_chunk(self, text, max_length):
        if len(text) <= max_length:
            return [text]

        chunks = []
        start = 0

        # Extended separator list with priorities
        separators = [
            ('\n\n', 2),  # Paragraph breaks
            ('\n', 2),    # Line breaks
            ('. ', 1),    # Sentence endings
            ('! ', 1),
            ('? ', 1),
            ('; ', 1),
            (': ', 1),    # Additional separators
            (', ', 0),    # Clause separators
            (' - ', 0),   # Dashes
            (' – ', 0),   # En dash
            (' — ', 0),   # Em dash
            (' ', 0),     # Word boundaries
        ]

        while start < len(text):
            end = start + max_length
            if end >= len(text):
                chunks.append(text[start:].strip())
                break

            best_pos = -1
            best_priority = -1

            # Find best separator within range
            for sep, priority in separators:
                pos = text.rfind(sep, start, end)
                if pos > start and (priority > best_priority or (priority == best_priority and pos > best_pos)):
                    best_pos = pos
                    best_priority = priority

            # If no good separator found, force split at word boundary
            if best_pos <= start:
                space_pos = text.find(' ', start + max_length // 2)
                if space_pos != -1 and space_pos < end:
                    best_pos = space_pos
                else:
                    best_pos = end - 1

            chunk = text[start:best_pos + 1].strip()
            if chunk:
                chunks.append(chunk)

            start = best_pos + 1
            while start < len(text) and text[start].isspace():
                start += 1

        return chunks

class TTSClient:
    """Handles communication with the TTS service."""

    def __init__(self, service_url=TTS_SERVICE_URL):
        self.service_url = service_url

    def generate_speech(self, text, speaker_id="Sofia Hellen", language_id="ru", style_wav="", max_retries=MAX_RETRIES):
        """Generate speech from text with retry logic using POST request."""
        payload = {
            'text': text,
            'speaker_id': speaker_id,
            'language_id': language_id,
            'style_wav': style_wav
        }

        headers = {
            'Content-Type': 'application/x-www-form-urlencoded'
        }

        for attempt in range(max_retries):
            try:
                logger.info(f"Attempting TTS generation (attempt {attempt + 1}/{max_retries}), text: {text[:10]}..., length: {len(text)}")
                response = requests.post(self.service_url, data=payload, headers=headers, timeout=30)

                if response.status_code == 200:
                    return response.content
                else:
                    logger.warning(f"TTS service returned status {response.status_code}: {response.text}")

            except Exception as e:
                logger.error(f"TTS request failed (attempt {attempt + 1}): {str(e)}")

            if attempt < max_retries - 1:
                time.sleep(RETRY_DELAY * (attempt + 1))  # Exponential backoff

        raise Exception(f"Failed to generate speech after {max_retries} attempts")


class AudioProcessor:
    """A class to handle all audio processing tasks."""

    def enhance_and_load_segment(self, file_stream, sr_target=44100, settings=None):
        """
        Enhances a single audio file stream and returns it as a pydub AudioSegment.
        Optional settings control which enhancement steps are applied.
        """
        try:
            # Default settings: all enhancements disabled
            settings = settings or {
                'resample': False,
                'noise_reduction': False,
                'dynamic_compression': False,
                'loudness_normalization': False
            }

            # Load audio data from the stream
            audio_data, sr_native = librosa.load(file_stream, sr=None)

            # 1. Resample if necessary
            sr = sr_native
            if settings.get('resample', False) and sr_native != sr_target:
                logger.info(f"Resampling from {sr_native}Hz to {sr_target}Hz.")
                audio_data = librosa.resample(y=audio_data, orig_sr=sr_native, target_sr=sr_target)
                sr = sr_target

            # 2. Noise Reduction
            if settings.get('noise_reduction', False):
                logger.info("Applying noise reduction.")
                audio_data = nr.reduce_noise(y=audio_data, sr=sr, stationary=False)

            # 3. Dynamic Range Compression
            if settings.get('dynamic_compression', False):
                logger.info("Applying dynamic range compression.")
                audio_data = self._dynamic_compression(audio_data)

            # 4. Loudness Normalization
            if settings.get('loudness_normalization', False):
                logger.info("Normalizing loudness.")
                audio_data = self._loudness_normalize(audio_data, sr)

            # 5. Convert processed numpy array to pydub AudioSegment
            logger.info("Creating audio segment.")
            return self._numpy_to_audiosegment(audio_data, sr)

        except Exception as e:
            logger.error(f"Error during audio enhancement: {str(e)}")
            logger.error(traceback.format_exc())
            return None

    def enhance_wav_bytes(self, wav_bytes, sr_target=44100, settings=None):
        """
        Enhances WAV bytes and returns AudioSegment.
        Optional settings control which enhancement steps are applied.
        """
        try:
            # Default settings: all enhancements disabled
            settings = settings or {
                'resample': False,
                'noise_reduction': False,
                'dynamic_compression': False,
                'loudness_normalization': False
            }

            # Create temporary file from bytes
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_file:
                tmp_file.write(wav_bytes)
                tmp_path = tmp_file.name

            try:
                # Load audio data from temporary file
                audio_data, sr_native = librosa.load(tmp_path, sr=None)

                # Process the audio
                sr = sr_native
                if settings.get('resample', False) and sr_native != sr_target:
                    audio_data = librosa.resample(y=audio_data, orig_sr=sr_native, target_sr=sr_target)
                    sr = sr_target

                if settings.get('noise_reduction', False):
                    audio_data = nr.reduce_noise(y=audio_data, sr=sr, stationary=False)

                if settings.get('dynamic_compression', False):
                    audio_data = self._dynamic_compression(audio_data)

                if settings.get('loudness_normalization', False):
                    audio_data = self._loudness_normalize(audio_data, sr)

                return self._numpy_to_audiosegment(audio_data, sr)

            finally:
                # Clean up temporary file
                os.unlink(tmp_path)

        except Exception as e:
            logger.error(f"Error during WAV bytes enhancement: {str(e)}")
            return None

    def _dynamic_compression(self, audio):
        """
        Applies dynamic range compression to even out volume levels.
        This is a simplified compressor that boosts quieter parts.
        """
        return np.tanh(audio * 1.5) * 0.9

    def _loudness_normalize(self, audio, sr, target_lufs=-23.0):
        """
        Normalizes audio to a target LUFS level.
        """
        meter = pyln.Meter(sr)
        loudness = meter.integrated_loudness(audio)

        if loudness == float('-inf'):
            logger.warning("Input audio is silent, skipping loudness normalization.")
            return audio

        return pyln.normalize.loudness(audio, loudness, target_lufs)

    def _numpy_to_audiosegment(self, audio_data, sr):
        """
        Converts a NumPy audio array to a pydub AudioSegment.
        """
        audio_int16 = (audio_data * 32767).astype(np.int16)
        return AudioSegment(
            audio_int16.tobytes(),
            frame_rate=sr,
            sample_width=audio_int16.dtype.itemsize,
            channels=1  # Assuming mono audio
        )

class TTSProcessor:
    """Main class that orchestrates the TTS processing workflow."""

    def __init__(self):
        self.chunker = TextChunker()
        self.tts_client = TTSClient()
        self.audio_processor = AudioProcessor()

    def process_text_to_audio(self, text, speaker_id="Sofia Hellen", language_id="ru", style_wav="", max_workers=1):
        """
        Process text through the complete TTS pipeline.
        Returns merged AudioSegment.
        """
        # Split text into chunks
        chunks = self.chunker.split_text(text)
        if not chunks:
            raise ValueError("No valid text chunks found")

        logger.info(f"Processing {len(chunks)} text chunks")

        def process_chunk(chunk_data):
            chunk_idx, chunk_text = chunk_data
            try:
                logger.info(f"Processing chunk {chunk_idx + 1}/{len(chunks)}, text: {chunk_text[:10]}..., length: {len(chunk_text)}")
                wav_bytes = self.tts_client.generate_speech(
                    chunk_text, speaker_id, language_id, style_wav
                )

                # Process the audio
                segment = self.audio_processor.enhance_wav_bytes(wav_bytes)
                if segment:
                    return (chunk_idx, segment)
                else:
                    logger.error(f"Failed to process audio for chunk {chunk_idx + 1}")
                    return (chunk_idx, None)

            except Exception as e:
                logger.error(f"Error processing chunk {chunk_idx + 1}: {str(e)}")
                return (chunk_idx, None)

        # Process chunks with threading
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(process_chunk, (i, chunk)): i
                for i, chunk in enumerate(chunks)
            }

            results = {}
            for future in as_completed(futures):
                chunk_idx, segment = future.result()
                results[chunk_idx] = segment

        # Merge segments in order
        merged_audio = AudioSegment.empty()
        successful_segments = 0

        for i in range(len(chunks)):
            if i in results and results[i] is not None:
                merged_audio += results[i]
                successful_segments += 1
            else:
                logger.warning(f"Failed to process chunk {i + 1}")
                raise Exception("Chunks process failed")

        if successful_segments == 0:
            raise Exception("All chunks failed to process")

        logger.info(f"Successfully processed {successful_segments}/{len(chunks)} chunks")
        return merged_audio

    def process_text_to_raw_wavs(self, text, speaker_id="Sofia Hellen", language_id="ru", style_wav="", max_workers=1):
        """
        Processes text into multiple raw WAV audio chunks without enhancement or merging.
        Returns a list of WAV file contents (bytes).
        """
        chunks = self.chunker.split_text(text)
        if not chunks:
            raise ValueError("No valid text chunks found")

        logger.info(f"Generating raw WAVs for {len(chunks)} text chunks")

        def generate_wav_for_chunk(chunk_data):
            chunk_idx, chunk_text = chunk_data
            try:
                logger.info(f"Generating WAV for chunk {chunk_idx + 1}/{len(chunks)}, text: {chunk_text[:10]}...")
                # Directly get the raw wav bytes from the TTS client
                wav_bytes = self.tts_client.generate_speech(
                    chunk_text, speaker_id, language_id, style_wav
                )
                return (chunk_idx, wav_bytes)
            except Exception as e:
                logger.error(f"Error generating WAV for chunk {chunk_idx + 1}: {str(e)}")
                return (chunk_idx, None)

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(generate_wav_for_chunk, (i, chunk)): i
                for i, chunk in enumerate(chunks)
            }
            results = {}
            for future in as_completed(futures):
                chunk_idx, wav_content = future.result()
                results[chunk_idx] = wav_content

        # Collect results in order, ensuring all chunks were processed
        wav_files_content = []
        for i in range(len(chunks)):
            if i in results and results[i] is not None:
                wav_files_content.append(results[i])
            else:
                logger.warning(f"Failed to generate WAV for chunk {i + 1}")
                raise Exception(f"Chunk {i+1} failed to process, aborting.")

        logger.info(f"Successfully generated {len(wav_files_content)}/{len(chunks)} raw WAV files.")
        return wav_files_content

# Instantiate components
processor = AudioProcessor()
tts_processor = TTSProcessor()

@app.route('/health', methods=['GET'])
@app.route('/api/health', methods=['GET'])
def health_check():
    """Endpoint to check if the service is running."""
    return jsonify({'status': 'healthy', 'timestamp': datetime.now().isoformat()})

@app.route('/api/convert', methods=['POST'])
def process_and_merge_files():
    """
    Main endpoint to process, enhance, and merge multiple audio files.
    Accepts multiple 'files' in a multipart/form-data request.
    Returns a single, merged, and enhanced MP3 file.
    """
    try:
        # 1. --- File Validation ---
        if 'files' not in request.files:
            return jsonify({'error': 'No file part in the request. Use key "files".'}), 400

        files = request.files.getlist('files')

        if not files or all(f.filename == '' for f in files):
            return jsonify({'error': 'No files selected for uploading.'}), 400

        valid_files = []
        for file_stream in files:
            if file_stream.filename.lower().endswith('.zip'):
                valid_files.extend(extract_zip(file_stream))
            elif allowed_file(file_stream.filename):
                valid_files.append(file_stream)

        if not valid_files:
            return jsonify({'error': f'No valid files found. Only {", ".join(ALLOWED_EXTENSIONS)} are allowed.'}), 400

        logger.info(f"Received {len(valid_files)} valid files for processing.")

        # 2. --- Audio Processing ---
        enhanced_segments = []
        for file_stream in valid_files:
            logger.info(f"Processing file: {secure_filename(file_stream.name)}")
            segment = processor.enhance_and_load_segment(file_stream)
            if segment:
                enhanced_segments.append(segment)
            else:
                logger.error(f"Failed to process {secure_filename(file_stream.name)}. Skipping.")

        if not enhanced_segments:
            return jsonify({'error': 'Audio processing failed for all provided files.'}), 500

        # 3. --- Merging Audio Segments ---
        logger.info("Merging all processed audio segments.")
        merged_audio = AudioSegment.empty()
        for segment in enhanced_segments:
            merged_audio += segment

        # 4. --- Exporting to a Temporary File ---
        bitrate = request.form.get('bitrate', '192k')

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            output_path = tmp_file.name

        logger.info(f"Exporting merged audio to temporary file: {output_path} with bitrate {bitrate}.")
        merged_audio.export(output_path, format="mp3", bitrate=bitrate)

        # 5. --- Sending File and Scheduling Cleanup ---
        @after_this_request
        def cleanup(response):
            try:
                logger.info(f"Cleaning up temporary file: {output_path}")
                os.remove(output_path)
            except Exception as e:
                logger.error(f"Error during file cleanup: {str(e)}")
            return response

        logger.info("Sending final merged MP3 file to client.")
        return send_file(
            output_path,
            as_attachment=True,
            download_name=f"enhanced_merged_{uuid.uuid4().hex[:8]}.mp3",
            mimetype='audio/mpeg'
        )

    except Exception as e:
        logger.error(f"An unexpected error occurred in /convert: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': 'An internal server error occurred.'}), 500

@app.route('/api/tts', methods=['POST'])
def text_to_speech():
    """
    Endpoint for text-to-speech processing.
    Accepts text and TTS parameters, returns enhanced merged MP3.
    """
    try:
        # Get request data
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        text = data.get('text', '').strip()
        if not text:
            return jsonify({'error': 'No text provided'}), 400

        # Optional TTS parameters
        speaker_id = data.get('speaker_id', 'Sofia Hellen')
        language_id = data.get('language_id', 'ru')
        style_wav = data.get('style_wav', '')
        bitrate = data.get('bitrate', '192k')
        max_workers = data.get('max_workers', 1)

        logger.info(f"Processing TTS request for text length: {len(text)} characters")

        # Process text through TTS pipeline
        merged_audio = tts_processor.process_text_to_audio(
            text, speaker_id, language_id, style_wav, max_workers
        )

        # Export to temporary file
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tmp_file:
            output_path = tmp_file.name

        logger.info(f"Exporting TTS result to temporary file: {output_path} with bitrate {bitrate}")
        merged_audio.export(output_path, format="mp3", bitrate=bitrate)

        # Setup cleanup
        @after_this_request
        def cleanup(response):
            try:
                logger.info(f"Cleaning up temporary file: {output_path}")
                os.remove(output_path)
            except Exception as e:
                logger.error(f"Error during file cleanup: {str(e)}")
            return response

        logger.info("Sending TTS generated MP3 file to client.")
        return send_file(
            output_path,
            as_attachment=True,
            download_name=f"tts_output_{uuid.uuid4().hex[:8]}.mp3",
            mimetype='audio/mpeg'
        )

    except Exception as e:
        logger.error(f"An unexpected error occurred in /tts: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': 'An internal server error occurred.'}), 500

@app.route('/api/tts/preview', methods=['POST'])
def text_to_speech_preview():
    """
    Preview endpoint that shows how text will be chunked without processing.
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        text = data.get('text', '').strip()
        if not text:
            return jsonify({'error': 'No text provided'}), 400

        chunker = TextChunker()
        chunks = chunker.split_text(text)

        return jsonify({
            'original_length': len(text),
            'num_chunks': len(chunks),
            'chunks': [
                {
                    'index': i,
                    'text': chunk,
                    'length': len(chunk)
                }
                for i, chunk in enumerate(chunks)
            ]
        })

    except Exception as e:
        logger.error(f"Error in /tts/preview: {str(e)}")
        return jsonify({'error': 'An internal server error occurred.'}), 500

@app.route('/api/tts_chunked', methods=['POST'])
def text_to_speech_chunked():
    """
    Endpoint for chunked text-to-speech.
    Accepts text and TTS parameters, returns a ZIP file containing raw WAV chunks.
    """
    temp_dir = None
    try:
        data = request.get_json()
        if not data:
            return jsonify({'error': 'No JSON data provided'}), 400

        text = data.get('text', '').strip()
        if not text:
            return jsonify({'error': 'No text provided'}), 400

        # Optional TTS parameters
        speaker_id = data.get('speaker_id', 'Sofia Hellen')
        language_id = data.get('language_id', 'ru')
        style_wav = data.get('style_wav', '')
        max_workers = data.get('max_workers', 1)

        logger.info(f"Processing chunked TTS request for text length: {len(text)} characters")

        # Get raw wav bytes for each chunk
        wav_chunks = tts_processor.process_text_to_raw_wavs(
            text, speaker_id, language_id, style_wav, max_workers
        )

        # Create a temporary directory for the WAV files and the ZIP archive
        temp_dir = tempfile.mkdtemp()
        zip_path = os.path.join(temp_dir, 'tts_chunks.zip')

        logger.info(f"Creating ZIP archive at {zip_path}")
        with zipfile.ZipFile(zip_path, 'w') as zipf:
            for i, wav_data in enumerate(wav_chunks):
                # We write the wav data directly to the zip archive
                # arcname is the name of the file inside the zip
                zipf.writestr(f'chunk_{i+1}.wav', wav_data)

        # Keep track of the directory to be cleaned up
        cleanup_dir = temp_dir

        @after_this_request
        def cleanup(response):
            try:
                logger.info(f"Cleaning up temporary directory: {cleanup_dir}")
                shutil.rmtree(cleanup_dir)
            except Exception as e:
                logger.error(f"Error during directory cleanup: {str(e)}")
            return response

        logger.info("Sending TTS generated ZIP file to client.")
        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f"tts_chunks_{uuid.uuid4().hex[:8]}.zip",
            mimetype='application/zip'
        )

    except Exception as e:
        # If an error occurred and the temp_dir was created, clean it up
        if temp_dir and os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

        logger.error(f"An unexpected error occurred in /api/tts_chunked: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({'error': 'An internal server error occurred.'}), 500

@app.route('/')
def index():
    """Render the main page of the application."""
    return render_template('index.html')

if __name__ == '__main__':
    print(f"Prepare to work with: {TTS_SERVICE_URL}")

    # Use a production-ready WSGI server like Gunicorn or Waitress instead of app.run in production
    app.run(host='0.0.0.0', port=5000, debug=False)