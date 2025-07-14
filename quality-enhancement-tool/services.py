# services.py
import os
import subprocess
import logging
import requests
import uuid
import zipfile
import json
import shutil
from werkzeug.utils import secure_filename
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, APIC, TCON, TLAN, TRCK, TPUB, TCOP, COMM
from mutagen.mp3 import MP3

from errors import AppError, FFmpegError, TTSError
from config import PRESETS
import time

logger = logging.getLogger(__name__)

class FFmpegService:
    """A service class to encapsulate all FFmpeg-related operations."""

    def is_available(self):
        """Checks if FFmpeg is installed and accessible in the system's PATH."""
        return shutil.which("ffmpeg") is not None

    def check_availability(self):
        """Raises an error if FFmpeg is not available."""
        if not self.is_available():
            raise FFmpegError("FFmpeg is not installed or not found in system PATH.", 503)

    def _run_command(self, cmd, error_msg="FFmpeg command failed"):
        """
        A private helper to run a shell command and handle potential errors.

        Args:
            cmd (list): The command and its arguments as a list of strings.
            error_msg (str): A custom message for the error log.

        Raises:
            FFmpegError: If the command fails or FFmpeg is not found.
        """
        try:
            # Using capture_output=True to get stdout/stderr
            process = subprocess.run(
                cmd, capture_output=True, text=True, check=True, encoding='utf-8'
            )
            logger.info(f"FFmpeg command successful: {' '.join(cmd)}")
            return process
        except FileNotFoundError:
            logger.error("FFMPEG_ERROR: The ffmpeg command was not found.")
            raise FFmpegError("FFmpeg not found. Please ensure it is installed and in the system PATH.", 503)
        except subprocess.CalledProcessError as e:
            # Log the detailed error from stderr for debugging
            logger.error(f"{error_msg}: {e.stderr}")
            raise FFmpegError(f"An error occurred during video processing: {e.stderr}", 500)

    def extract_audio_stream(self, input_path, output_dir, output_filename="extracted.mp3"):
        """
        Extracts the primary audio stream from a media file, re-encoding to MP3.
        This helps normalize files and remove non-audio tracks.

        Returns:
            str: The path to the extracted audio file.
        """
        output_path = os.path.join(output_dir, output_filename)
        cmd = [
            'ffmpeg', '-i', input_path, '-vn', '-map', '0:a:0',
            '-c:a', 'libmp3lame', '-q:a', '0', '-y', output_path
        ]
        logger.info(f"Extracting audio from {os.path.basename(input_path)}...")
        self._run_command(cmd, "FFmpeg audio extraction failed")
        return output_path

    def enhance_audio(self, input_path, output_dir, preset_name='music', output_filename="enhanced.mp3"):
        """
        Applies audio enhancement filters using FFmpeg based on a defined preset.

        Returns:
            str: The path to the enhanced audio file.
        """
        config = PRESETS.get(preset_name)
        if not config:
            raise AppError(f"Invalid enhancement preset: {preset_name}", 400)

        output_path = os.path.join(output_dir, output_filename)
        ln = config['loudnorm']
        loudnorm_filter = f"loudnorm=I={ln['I']}:LRA={ln['LRA']}:TP={ln['TP']}"
        cmd = [
            'ffmpeg', '-i', input_path, '-af', loudnorm_filter,
            '-c:a', 'libmp3lame', '-b:a', config['bitrate'], '-ar', config['sample_rate'],
            '-ac', config['channels'], '-y', output_path
        ]
        logger.info(f"Enhancing {os.path.basename(input_path)} with preset '{preset_name}'...")
        self._run_command(cmd, "FFmpeg audio enhancement failed")
        return output_path

    def join_files(self, file_paths, output_dir, output_filename):
        """

        Joins multiple MP3 files into a single file using FFmpeg's concat demuxer.

        Returns:
            str: The path to the joined audio file.
        """
        if len(file_paths) < 2:
            raise AppError("At least two files are required for joining.", 400)

        output_path = os.path.join(output_dir, output_filename)
        concat_list_path = os.path.join(output_dir, "concat_list.txt")

        try:
            # Create a temporary file list for FFmpeg's concat demuxer
            with open(concat_list_path, 'w', encoding='utf-8') as f:
                for path in file_paths:
                    f.write(f"file '{os.path.abspath(path)}'\n")

            cmd = [
                'ffmpeg', '-f', 'concat', '-safe', '0', '-i',
                concat_list_path, '-c', 'copy', '-y', output_path
            ]
            logger.info(f"Joining {len(file_paths)} files into {output_filename}...")
            self._run_command(cmd, "FFmpeg file joining failed")
            return output_path
        finally:
            # Ensure the temporary list file is removed
            if os.path.exists(concat_list_path):
                os.remove(concat_list_path)

class ID3TagService:
    """Manages reading and writing ID3 tags for MP3 files using Mutagen."""

    def __init__(self):
        """Initializes the service with mappings from metadata keys to ID3 tag classes."""
        self._TAG_MAP = {
            'title': TIT2, 'artist': TPE1, 'album': TALB, 'year': TDRC,
            'genre': TCON, 'language': TLAN, 'track': TRCK, 'publisher': TPUB,
            'copyright': TCOP
        }
        self._REVERSE_TAG_MAP = {
            'TIT2': 'title', 'TPE1': 'artist', 'TALB': 'album', 'TDRC': 'year',
            'TCON': 'genre', 'TLAN': 'language', 'TRCK': 'track', 'TPUB': 'publisher',
            'TCOP': 'copyright'
        }

    def add_tags(self, file_path, metadata):
        """
        Adds or overwrites ID3 tags on an MP3 file. It clears all existing tags first.

        Args:
            file_path (str): The path to the MP3 file.
            metadata (dict): A dictionary of metadata to add.
        """
        if not metadata:
            return

        try:
            audio = MP3(file_path, ID3=ID3)
            audio.delete()  # Clear all existing tags to ensure a clean slate

            # Add standard text-based tags
            for key, tag_class in self._TAG_MAP.items():
                if metadata.get(key):
                    audio[tag_class.__name__] = tag_class(encoding=3, text=str(metadata[key]))

            # Add comments tag
            if metadata.get('comments'):
                audio['COMM::eng'] = COMM(encoding=3, lang='eng', desc='', text=str(metadata['comments']))

            # Add cover art
            if metadata.get('cover_art'):
                audio['APIC'] = APIC(
                    encoding=3, mime='image/jpeg', type=3,
                    desc='Cover', data=metadata['cover_art']
                )

            audio.save()
            logger.info(f"ID3 tags successfully added to {os.path.basename(file_path)}")
        except Exception as e:
            logger.error(f"Failed to add ID3 tags to {file_path}: {e}", exc_info=True)
            # This is not a fatal error, so we log it but don't raise an exception

    def extract_tags(self, file_path):
        """
        Extracts ID3 tags from an MP3 file into a dictionary.

        Returns:
            dict: A dictionary of the extracted tags.
        """
        tags = {}
        try:
            audio = MP3(file_path, ID3=ID3)
            if not audio.tags:
                return tags

            # Extract standard tags
            for tag_id, key in self._REVERSE_TAG_MAP.items():
                if tag_id in audio:
                    value = audio[tag_id].text[0] if audio[tag_id].text else ""
                    tags[key] = str(value)

            # Extract comments and cover art
            for frame_id in audio:
                if frame_id.startswith('COMM') and audio[frame_id].text:
                    tags['comments'] = audio[frame_id].text[0]
                elif frame_id.startswith('APIC'):
                    tags['cover_art'] = audio[frame_id].data

            logger.info(f"Extracted {len(tags)} ID3 tags from {os.path.basename(file_path)}")
            return tags
        except Exception as e:
            logger.error(f"Failed to extract ID3 tags from {file_path}: {e}", exc_info=True)
            return {}

class TTSService:
    """Service to interact with an external Text-to-Speech (TTS) server."""

    def __init__(self, server_url):
        self.server_url = server_url
        self.timeout = 300  # Request timeout in seconds(5min)

    def generate_audio(self, text, voice, save_directory):
        """
        Requests TTS audio from the server and saves it to a file.

        Returns:
            str: The path to the saved TTS audio file.

        Raises:
            TTSError: If the request to the TTS server fails.
        """
        payload = {"text": text, "voice": voice}
        output_path = os.path.join(save_directory, f"tts_{uuid.uuid4()}.mp3")

        start_time = time.time()

        try:
            response = requests.post(self.server_url, json=payload, stream=True, timeout=self.timeout)
            response.raise_for_status()

            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)

            elapsed_time = time.time() - start_time
            logger.info(f"TTS audio saved to {os.path.basename(output_path)} in {elapsed_time:.2f} seconds")
            return output_path
        except requests.exceptions.RequestException as e:
            elapsed_time = time.time() - start_time
            logger.error(f"TTS server communication failed after {elapsed_time:.2f} seconds: {e}")
            raise TTSError(f"Could not connect to the TTS service: {e}", 502)

class FileService:
    """Handles file-related operations like saving, zipping, and metadata extraction."""

    def __init__(self, upload_folder):
        self.upload_folder = upload_folder

    def save_uploaded_file(self, file_storage, directory):
        """
        Saves a Flask FileStorage object to a specified directory.

        Args:
            file_storage (werkzeug.FileStorage): The file object from the request.
            directory (str): The directory where the file should be saved.

        Returns:
            str: The full path to the saved file.
        """
        filename = secure_filename(file_storage.filename)
        save_path = os.path.join(directory, filename)
        file_storage.save(save_path)
        logger.info(f"Saved uploaded file to {save_path}")
        return save_path

    def get_metadata_from_request(self, req):
        """
        Parses metadata from a Flask request, including form fields and cover art file.

        Returns:
            dict: A dictionary of metadata.
        """
        metadata = {}
        # Load metadata from a JSON string in a form field
        if "metadata" in req.form:
            try:
                metadata = json.loads(req.form.get("metadata", "{}"))
            except json.JSONDecodeError:
                raise AppError("Invalid JSON format in the 'metadata' field.", 400)

        # Add cover art if it was uploaded
        if 'cover_art' in req.files and req.files['cover_art'].filename:
            metadata['cover_art'] = req.files['cover_art'].read()
            logger.info("Cover art included in the request.")

        # Filter out any keys with empty values
        return {k: v for k, v in metadata.items() if v}

    def get_file_metadata(self, file_path):
        """
        Collects technical metadata (size, duration, etc.) from an audio file.

        Returns:
            dict: A dictionary of file properties.
        """
        try:
            audio = MP3(file_path)
            id3_service = ID3TagService()
            metadata = {
                'filename': os.path.basename(file_path),
                'file_size_bytes': os.path.getsize(file_path),
                'duration_seconds': round(audio.info.length, 2),
                'bitrate_bps': audio.info.bitrate,
                'sample_rate_hz': audio.info.sample_rate,
                'channels': audio.info.channels,
                'format': 'mp3',
                'id3_tags': id3_service.extract_tags(file_path)
            }
            # Remove cover art from metadata to keep JSON small
            metadata['id3_tags'].pop('cover_art', None)
            return metadata
        except Exception as e:
            logger.error(f"Failed to collect file metadata for {file_path}: {e}", exc_info=True)
            return {"error": "Could not read file metadata."}

    def create_zip_archive(self, file_paths, output_dir, zip_name):
        """
        Creates a zip archive from a list of files.

        Args:
            file_paths (list): A list of paths to the files to be included.
            output_dir (str): The directory where the zip file will be saved.
            zip_name (str): The base name for the zip file (without .zip extension).

        Returns:
            str: The path to the created zip archive.
        """
        zip_path_base = os.path.join(output_dir, zip_name)

        with zipfile.ZipFile(f"{zip_path_base}.zip", 'w', zipfile.ZIP_DEFLATED) as zipf:
            for file_path in file_paths:
                # Add file to the zip with its base name
                zipf.write(file_path, os.path.basename(file_path))

        logger.info(f"Created zip archive at {zip_path_base}.zip")
        return f"{zip_path_base}.zip"

    def create_zip_with_metadata(self, file_path, metadata, temp_dir):
        """
        Creates a zip file containing an audio file and its corresponding metadata JSON file.

        Returns:
            str: The path to the created zip archive.
        """
        base_filename = os.path.splitext(os.path.basename(file_path))[0]
        zip_path = os.path.join(temp_dir, f"{base_filename}.zip")

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                # Add the audio file
                zipf.write(file_path, os.path.basename(file_path))
                # Add the metadata as a JSON file
                metadata_json = json.dumps(metadata, indent=2, default=str)
                zipf.writestr(f"{base_filename}_metadata.json", metadata_json)

            logger.info(f"Created zip with metadata at {zip_path}")
            return zip_path
        except Exception as e:
            logger.error(f"Failed to create zip with metadata: {e}", exc_info=True)
            raise AppError("Failed to create the zip archive.", 500)

    def unpack_files_from_request(self, req, temp_dir):
        """
        Extracts MP3 files from a request, supporting both multi-file uploads
        and single ZIP file uploads.

        Returns:
            list: A sorted list of paths to the MP3 files.
        """
        temp_files = []
        # Case 1: A single ZIP file is uploaded
        if 'file' in req.files and req.files['file'].filename.lower().endswith('.zip'):
            zip_file = req.files['file']
            zip_path = self.save_uploaded_file(zip_file, temp_dir)
            extract_dir = os.path.join(temp_dir, 'zip_extract')
            os.makedirs(extract_dir)

            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(extract_dir)

            for root, _, files in os.walk(extract_dir):
                for f in files:
                    if f.lower().endswith('.mp3'):
                        temp_files.append(os.path.join(root, f))

        # Case 2: Multiple MP3 files are uploaded directly
        else:
            files = req.files.getlist('files')
            if not files:
                raise AppError('No files or ZIP archive were uploaded.', 400)

            for i, file in enumerate(files):
                if file and file.filename.lower().endswith('.mp3'):
                    # Prepend index to maintain order
                    filename = f"{i:03d}_{secure_filename(file.filename)}"
                    temp_path = os.path.join(temp_dir, filename)
                    file.save(temp_path)
                    temp_files.append(temp_path)

        temp_files.sort()  # Sort to ensure predictable order
        return temp_files