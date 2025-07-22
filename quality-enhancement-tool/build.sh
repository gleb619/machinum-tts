#!/bin/bash

echo "Starting..."

# Check if Python is installed
if ! command -v python3 &> /dev/null; then
    echo "Error: Python 3 is not installed"
    exit 1
fi

# Check if pip is installed
if ! command -v pip3 &> /dev/null; then
    echo "Error: pip3 is not installed"
    exit 1
fi

# Install Python dependencies
echo "Installing Python dependencies..."
pip3 install -r requirements.txt

# Check if FFmpeg is installed
if ! command -v ffmpeg &> /dev/null; then
    echo "Warning: FFmpeg is not installed. Installing..."

    # Detect OS and install FFmpeg
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        # Linux
        sudo apt-get update
        sudo apt-get install -y ffmpeg sox libsox-fmt-all
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        # macOS
        if command -v brew &> /dev/null; then
            brew install ffmpeg sox
        else
            echo "Error: Homebrew not found. Please install FFmpeg manually."
            exit 1
        fi
    else
        echo "Error: Unsupported OS. Please install FFmpeg manually."
        exit 1
    fi
fi

# Verify FFmpeg installation
if ! command -v ffmpeg &> /dev/null; then
    echo "Error: FFmpeg installation failed"
    exit 1
fi

echo "FFmpeg version:"
ffmpeg -version | head -1

# Check if SoX is installed (optional)
if ! command -v sox &> /dev/null; then
    echo "Warning: SoX is not installed. Noise reduction will be skipped."
else
    echo "SoX version:"
    sox --version
fi

echo "Setup complete!"

export FLASK_DEBUG=true
export SERVER_URL=http://localhost:5001/api/tts

# Run the Flask application
python3 app.py