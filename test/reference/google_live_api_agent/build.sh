#!/bin/bash

# Gemini Live API Client - C Version Build Script
# This script automatically detects your system and builds the appropriate version

set -e

echo "🚀 Building Gemini Live API Client (C Version)"
echo "=============================================="

# Check if we're on macOS or Linux
if [[ "$OSTYPE" == "darwin"* ]]; then
    echo "📱 Detected macOS"
    PLATFORM="macos"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    echo "🐧 Detected Linux"
    PLATFORM="linux"
else
    echo "❌ Unsupported platform: $OSTYPE"
    exit 1
fi

# Check for required tools
echo "🔍 Checking build tools..."
if ! command -v gcc &> /dev/null; then
    echo "❌ GCC not found. Please install a C compiler."
    exit 1
fi

if ! command -v make &> /dev/null; then
    echo "❌ Make not found. Please install make."
    exit 1
fi

echo "✅ Build tools found"

# Check for dependencies
echo "🔍 Checking dependencies..."

# Function to check if a library is available
check_lib() {
    local lib=$1
    local name=$2
    
    if pkg-config --exists $lib 2>/dev/null; then
        echo "✅ $name found via pkg-config"
        return 0
    elif [[ "$PLATFORM" == "macos" ]]; then
        if brew list $lib &>/dev/null; then
            echo "✅ $name found via Homebrew"
            return 0
        fi
    fi
    
    echo "❌ $name not found"
    return 1
}

# Check each dependency
DEPS_OK=true
check_lib "openssl" "OpenSSL" || DEPS_OK=false
check_lib "portaudio-2.0" "PortAudio" || DEPS_OK=false
check_lib "json-c" "json-c" || DEPS_OK=false

if [[ "$DEPS_OK" == "false" ]]; then
    echo ""
    echo "📦 Installing dependencies..."
    if [[ "$PLATFORM" == "macos" ]]; then
        echo "Installing via Homebrew..."
        brew install portaudio openssl json-c
    else
        echo "Installing via package manager..."
        sudo apt-get update
        sudo apt-get install -y libportaudio2 libportaudio-dev libssl-dev libjson-c-dev
    fi
    echo "✅ Dependencies installed"
else
    echo "✅ All dependencies found"
fi

# Check for API key
if [[ -z "$GOOGLE_API_KEY" ]]; then
    echo ""
    echo "⚠️  Warning: GOOGLE_API_KEY environment variable not set"
    echo "   Please set it before running the program:"
    echo "   export GOOGLE_API_KEY='your_api_key_here'"
    echo ""
fi

# Build the simplified version (more likely to work)
echo ""
echo "🔨 Building simplified version..."
make simple

if [[ $? -eq 0 ]]; then
    echo "✅ Build successful!"
    echo ""
    echo "🎯 To run the program:"
    echo "   ./gemini_live_c_simple"
    echo ""
    echo "📖 For more options, run: make help"
else
    echo "❌ Build failed. Trying full version..."
    make all
    
    if [[ $? -eq 0 ]]; then
        echo "✅ Full version build successful!"
        echo ""
        echo "🎯 To run the program:"
        echo "   ./gemini_live_c"
    else
        echo "❌ Both builds failed. Please check the error messages above."
        exit 1
    fi
fi

echo ""
echo "🎉 Build complete! Happy coding!"
