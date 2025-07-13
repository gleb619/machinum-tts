# machinum-tts

### Audio Processing and Coqui TTS API

The **Audio Processing and Text-to-Speech API** offers comprehensive audio processing capabilities along with
text-to-speech functionalities. It supports uploading and enhancing audio files, merging them into a single MP3 file,
generating speech from text with customizable parameters, previewing text chunking without processing, and creating raw
WAV chunks for detailed control over individual segments. The application includes multi-threading support to handle
multiple requests efficiently and secure file handling practices.  
[First app](audio-app/README.md)

### Edge TTS API

The **Edge TTS API** is a Flask-based web service that converts text into speech using the `edge_tts` library. It offers
a simple interface to generate audio files from text input, with options to specify voice models and output file names.
The application includes a health check endpoint to verify its status and a main page for demonstration purposes.  
[Second app](edge-tts/README.md)

### AudioForge

**AudioForge** is a Flask service designed to process audio files through various functionalities such as text-to-speech
conversion, audio enhancement, ID3 tagging, and MP3 file joining. Users can upload individual or multiple MP3 files,
apply quality enhancements using presets, add metadata including cover art, and combine files into a single output. The
service also provides health checks to ensure dependencies like FFmpeg are available.  
[Third app](quality-enhancement-tool/README.md)
