#!/bin/bash

cd audio-app
./build.sh &
cd -

cd edge-tts
./build.sh &
cd -

cd quality-enhancement-tool
./build.sh &
cd -
