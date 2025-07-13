#!/bin/bash

echo "Installing"

python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt

echo "Starting"

python app.py