# Gemini Live API Client - C Version

This is a C implementation of the Gemini Live API client that provides real-time audio streaming with Google's Gemini AI model.

## Features

- Real-time microphone input capture
- WebSocket connection to Gemini Live API
- Real-time audio output playback
- Multi-threaded architecture for smooth audio streaming
- SSL/TLS secure connection
- JSON message handling

## Prerequisites

### System Dependencies

#### macOS
```bash
# Install Homebrew if you haven't already
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install required libraries
brew install portaudio openssl json-c
```

#### Ubuntu/Debian Linux
```bash
sudo apt-get update
sudo apt-get install -y build-essential libportaudio2 libportaudio-dev libssl-dev libjson-c-dev
```

#### CentOS/RHEL/Fedora
```bash
# For CentOS/RHEL 7/8
sudo yum install -y gcc make portaudio-devel openssl-devel json-c-devel

# For Fedora
sudo dnf install -y gcc make portaudio-devel openssl-devel json-c-devel
```

### API Key Setup

1. Get your Google API key from [Google AI Studio](https://aistudio.google.com/)
2. Set the environment variable:
   ```bash
   export GOOGLE_API_KEY="your_api_key_here"
   ```
   
   Or add it to your shell profile:
   ```bash
   echo 'export GOOGLE_API_KEY="your_api_key_here"' >> ~/.zshrc
   source ~/.zshrc
   ```

## Building

### Quick Build
```bash
make all
```

### Install Dependencies and Build
```bash
make install-deps
make all
```

## Running

### Basic Run
```bash
make run
```

### Manual Run
```bash
./gemini_live_c
```

## Usage

1. **Start the program**: Run `./gemini_live_c`
2. **Wait for connection**: The program will connect to Gemini Live API
3. **Start speaking**: Once connected, speak into your microphone
4. **Listen to responses**: Gemini will respond with audio through your speakers
5. **Stop**: Press `Ctrl+C` to gracefully shut down

## Architecture

The program uses a multi-threaded architecture:

- **Main Thread**: Manages WebSocket connection and setup
- **Microphone Thread**: Captures audio from microphone and enqueues for sending
- **Speaker Thread**: Plays audio responses from the queue
- **WebSocket Thread**: Receives messages and extracts audio data

### Audio Flow

```
Microphone → Audio Queue → WebSocket → Gemini API
                                    ↓
Speaker ← Audio Queue ← WebSocket ← Gemini Response
```

## Configuration

You can modify these constants in `gemini_live_c.c`:

- `INPUT_SR`: Microphone sample rate (default: 16000 Hz)
- `OUTPUT_SR`: Speaker sample rate (default: 24000 Hz)
- `FRAME_MS`: Audio frame duration (default: 50ms)
- `VOICE`: Gemini voice (default: "Aoede")
- `MODEL`: Gemini model (default: "models/gemini-2.5-flash-preview-native-audio-dialog")

## Troubleshooting

### Common Issues

1. **"Failed to initialize PortAudio"**
   - Make sure PortAudio is installed: `brew install portaudio` (macOS) or `sudo apt-get install libportaudio-dev` (Linux)

2. **"Failed to initialize SSL"**
   - Ensure OpenSSL is installed: `brew install openssl` (macOS) or `sudo apt-get install libssl-dev` (Linux)

3. **"Failed to parse JSON"**
   - Install json-c: `brew install json-c` (macOS) or `sudo apt-get install libjson-c-dev` (Linux)

4. **Audio not working**
   - Check your microphone and speaker permissions
   - Ensure audio devices are not being used by other applications
   - Try different audio devices in your system settings

5. **WebSocket connection failed**
   - Verify your API key is correct
   - Check internet connection
   - Ensure the API key has access to Gemini Live API

### Debug Mode

The program includes extensive logging. If you encounter issues, check the console output for error messages.

## Performance

- **Latency**: Typically 100-200ms end-to-end
- **CPU Usage**: Moderate (varies with audio processing)
- **Memory Usage**: ~10-20MB
- **Network**: Requires stable internet connection

## Security

- Uses SSL/TLS encryption for all communications
- API key is read from environment variables (never hardcoded)
- No audio data is stored locally

## Limitations

- WebSocket frame size limited to 125 bytes (can be extended)
- Audio format: PCM 16-bit signed integer
- Single channel audio (mono)
- Fixed sample rates

## Extending

The modular design makes it easy to extend:

- Add support for different audio formats
- Implement audio effects or processing
- Add support for multiple voices
- Implement conversation history
- Add GUI interface

## License

This code is provided as-is for educational and development purposes.

## Support

For issues related to:
- **Google Gemini API**: Check [Google AI documentation](https://ai.google.dev/)
- **Audio issues**: Check PortAudio documentation
- **Build issues**: Ensure all dependencies are properly installed
