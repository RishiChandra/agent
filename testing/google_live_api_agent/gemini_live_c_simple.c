#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <netdb.h>
#include <openssl/ssl.h>
#include <openssl/err.h>
#include <portaudio.h>
#include <json-c/json.h>
#include <time.h>

// Configuration
#define API_KEY_ENV "GOOGLE_API_KEY"
#define WS_URL "generativelanguage.googleapis.com"
#define WS_PATH "/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
#define MODEL "models/gemini-2.5-flash-preview-native-audio-dialog"
#define VOICE "Aoede"
#define INPUT_SR 16000
#define OUTPUT_SR 24000
#define FRAME_MS 50
#define IN_BLOCK (INPUT_SR * FRAME_MS / 1000)
#define OUT_BLOCK (OUTPUT_SR * FRAME_MS / 1000)
#define BUFFER_SIZE 4096
#define MAX_QUEUE_SIZE 128

// Global state
volatile int running = 1;
volatile int setup_complete_received = 0;
SSL *ssl = NULL;
int sock = -1;
pthread_mutex_t audio_mutex = PTHREAD_MUTEX_INITIALIZER;
pthread_cond_t audio_cond = PTHREAD_COND_INITIALIZER;

// Audio queues
typedef struct {
    unsigned char *data;
    size_t size;
} audio_frame_t;

typedef struct {
    audio_frame_t *frames;
    int head;
    int tail;
    int size;
    int capacity;
    pthread_mutex_t mutex;
} audio_queue_t;

audio_queue_t mic_queue = {0};
audio_queue_t spk_queue = {0};

// Function declarations
void cleanup();
void signal_handler(int sig);
int init_ssl();
int connect_websocket();
int send_websocket_frame(const char *data, size_t len);
int receive_websocket_frame(char *buffer, size_t buffer_size);
void *mic_capture_thread(void *arg);
void *speaker_playback_thread(void *arg);
void *websocket_receive_thread(void *arg);
int init_audio_queues();
void cleanup_audio_queues();
int enqueue_audio(audio_queue_t *queue, const unsigned char *data, size_t size);
int dequeue_audio(audio_queue_t *queue, unsigned char *data, size_t *size);

// Base64 encoding/decoding (inline implementation)
static const char base64_chars[] = 
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

int base64_encode(const unsigned char *input, size_t input_len, char *output) {
    size_t i = 0, j = 0;
    unsigned char a, b, c;
    
    while (i < input_len) {
        a = i < input_len ? input[i++] : 0;
        b = i < input_len ? input[i++] : 0;
        c = i < input_len ? input[i++] : 0;
        
        output[j++] = base64_chars[a >> 2];
        output[j++] = base64_chars[((a & 3) << 4) | (b >> 4)];
        output[j++] = base64_chars[((b & 15) << 2) | (c >> 6)];
        output[j++] = base64_chars[c & 63];
    }
    
    // Add padding
    while (j % 4) {
        output[j++] = '=';
    }
    
    output[j] = '\0';
    return j;
}

int base64_decode(const char *input, unsigned char *output, size_t output_len) {
    size_t input_len = strlen(input);
    size_t i = 0, j = 0;
    unsigned char a, b, c, d;
    
    // Remove padding
    while (input_len > 0 && input[input_len - 1] == '=') {
        input_len--;
    }
    
    while (i < input_len) {
        // Find character in base64 table
        const char *p1 = strchr(base64_chars, input[i++]);
        const char *p2 = strchr(base64_chars, input[i++]);
        const char *p3 = strchr(base64_chars, input[i++]);
        const char *p4 = strchr(base64_chars, input[i++]);
        
        if (!p1 || !p2 || !p3 || !p4) return -1;
        
        a = p1 - base64_chars;
        b = p2 - base64_chars;
        c = p3 - base64_chars;
        d = p4 - base64_chars;
        
        if (j < output_len) output[j++] = (a << 2) | (b >> 4);
        if (j < output_len) output[j++] = ((b & 15) << 4) | (c >> 2);
        if (j < output_len) output[j++] = ((c & 3) << 6) | d;
    }
    
    return j;
}

// WebSocket frame creation
int create_websocket_frame(const char *payload, size_t payload_len, unsigned char *frame, size_t *frame_len) {
    if (payload_len > 65535) return -1; // Support up to 64KB frames
    
    frame[0] = 0x81; // FIN + text frame
    
    if (payload_len <= 125) {
        frame[1] = payload_len; // No MASK bit, just payload length
        memcpy(frame + 2, payload, payload_len);
        *frame_len = 2 + payload_len;
    } else {
        frame[1] = 126; // Extended payload length (16-bit)
        frame[2] = (payload_len >> 8) & 0xFF; // High byte
        frame[3] = payload_len & 0xFF;        // Low byte
        memcpy(frame + 4, payload, payload_len);
        *frame_len = 4 + payload_len;
    }
    
    return 0;
}

// Parse WebSocket frame
int parse_websocket_frame(const unsigned char *frame, size_t frame_len, char *payload, size_t *payload_len) {
    if (frame_len < 2) return -1;
    
    int opcode = frame[0] & 0x0F;
    int masked = (frame[1] & 0x80) != 0;
    int payload_length = frame[1] & 0x7F;
    
    if (opcode == 0x8) return -2; // Close frame
    if (opcode != 0x1) return -3; // Not a text frame
    
    int header_len = 2;
    if (payload_length == 126) {
        if (frame_len < 4) return -1;
        payload_length = (frame[2] << 8) | frame[3];
        header_len = 4;
    } else if (payload_length == 127) {
        if (frame_len < 10) return -1;
        payload_length = 0;
        for (int i = 0; i < 8; i++) {
            payload_length = (payload_length << 8) | frame[2 + i];
        }
        header_len = 10;
    }
    
    if (masked) {
        header_len += 4; // Skip mask
    }
    
    if (frame_len < header_len + payload_length) return -1;
    
    memcpy(payload, frame + header_len, payload_length);
    *payload_len = payload_length;
    
    return 0;
}

// Initialize SSL
int init_ssl() {
    SSL_library_init();
    SSL_load_error_strings();
    OpenSSL_add_ssl_algorithms();
    
    SSL_CTX *ctx = SSL_CTX_new(TLS_client_method());
    if (!ctx) {
        fprintf(stderr, "Failed to create SSL context\n");
        return -1;
    }
    
    ssl = SSL_new(ctx);
    if (!ssl) {
        fprintf(stderr, "Failed to create SSL connection\n");
        SSL_CTX_free(ctx);
        return -1;
    }
    
    SSL_CTX_free(ctx);
    return 0;
}

// Connect to WebSocket
int connect_websocket() {
    struct addrinfo hints, *result;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    
    if (getaddrinfo(WS_URL, "443", &hints, &result) != 0) {
        fprintf(stderr, "Failed to resolve hostname\n");
        return -1;
    }
    
    sock = socket(result->ai_family, result->ai_socktype, result->ai_protocol);
    if (sock < 0) {
        fprintf(stderr, "Failed to create socket\n");
        freeaddrinfo(result);
        return -1;
    }
    
    if (connect(sock, result->ai_addr, result->ai_addrlen) < 0) {
        fprintf(stderr, "Failed to connect\n");
        close(sock);
        freeaddrinfo(result);
        return -1;
    }
    
    freeaddrinfo(result);
    
    if (SSL_set_fd(ssl, sock) != 1) {
        fprintf(stderr, "Failed to set SSL file descriptor\n");
        return -1;
    }
    
    if (SSL_connect(ssl) != 1) {
        fprintf(stderr, "Failed to establish SSL connection\n");
        return -1;
    }
    
    // Send WebSocket handshake
    char api_key[256];
    const char *env_key = getenv(API_KEY_ENV);
    if (!env_key) {
        fprintf(stderr, "Please set %s environment variable\n", API_KEY_ENV);
        return -1;
    }
    snprintf(api_key, sizeof(api_key), "%s", env_key);
    
    char handshake[1024];
    snprintf(handshake, sizeof(handshake),
        "GET %s?key=%s HTTP/1.1\r\n"
        "Host: %s\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n",
        WS_PATH, api_key, WS_URL);
    
    printf("🤝 Sending WebSocket handshake:\n%s", handshake);
    
    if (SSL_write(ssl, handshake, strlen(handshake)) <= 0) {
        fprintf(stderr, "Failed to send WebSocket handshake\n");
        return -1;
    }
    
    // Read response (simplified)
    char response[1024];
    int bytes = SSL_read(ssl, response, sizeof(response) - 1);
    if (bytes <= 0) {
        fprintf(stderr, "Failed to read handshake response\n");
        return -1;
    }
    response[bytes] = '\0';
    
    printf("📥 Handshake response:\n%s\n", response);
    
    if (strstr(response, "101 Switching Protocols") == NULL) {
        fprintf(stderr, "WebSocket handshake failed\n");
        return -1;
    }
    
    printf("✅ Connected to Gemini Live API\n");
    return 0;
}

// Send WebSocket frame
int send_websocket_frame(const char *data, size_t len) {
    unsigned char frame[BUFFER_SIZE];
    size_t frame_len;
    
    printf("Creating WebSocket frame for %zu bytes\n", len);
    
    if (create_websocket_frame(data, len, frame, &frame_len) < 0) {
        fprintf(stderr, "Failed to create WebSocket frame\n");
        return -1;
    }
    
    printf("WebSocket frame created, total length: %zu bytes\n", frame_len);
    printf("Frame header: 0x%02x 0x%02x\n", frame[0], frame[1]);
    
    int result = SSL_write(ssl, frame, frame_len);
    printf("SSL_write result: %d\n", result);
    
    return result;
}

// Receive WebSocket frame
int receive_websocket_frame(char *buffer, size_t buffer_size) {
    unsigned char frame[BUFFER_SIZE];
    int bytes = SSL_read(ssl, frame, sizeof(frame));
    if (bytes <= 0) return bytes;
    
    size_t payload_len;
    return parse_websocket_frame(frame, bytes, buffer, &payload_len);
}

// Initialize audio queues
int init_audio_queues() {
    mic_queue.capacity = MAX_QUEUE_SIZE;
    mic_queue.frames = malloc(mic_queue.capacity * sizeof(audio_frame_t));
    if (!mic_queue.frames) return -1;
    
    spk_queue.capacity = MAX_QUEUE_SIZE;
    spk_queue.frames = malloc(spk_queue.capacity * sizeof(audio_frame_t));
    if (!spk_queue.frames) return -1;
    
    pthread_mutex_init(&mic_queue.mutex, NULL);
    pthread_mutex_init(&spk_queue.mutex, NULL);
    
    return 0;
}

// Cleanup audio queues
void cleanup_audio_queues() {
    pthread_mutex_lock(&mic_queue.mutex);
    for (int i = 0; i < mic_queue.capacity; i++) {
        if (mic_queue.frames[i].data) {
            free(mic_queue.frames[i].data);
        }
    }
    free(mic_queue.frames);
    pthread_mutex_unlock(&mic_queue.mutex);
    
    pthread_mutex_lock(&spk_queue.mutex);
    for (int i = 0; i < spk_queue.capacity; i++) {
        if (spk_queue.frames[i].data) {
            free(spk_queue.frames[i].data);
        }
    }
    free(spk_queue.frames);
    pthread_mutex_unlock(&spk_queue.mutex);
    
    pthread_mutex_destroy(&mic_queue.mutex);
    pthread_mutex_destroy(&spk_queue.mutex);
}

// Enqueue audio data
int enqueue_audio(audio_queue_t *queue, const unsigned char *data, size_t size) {
    pthread_mutex_lock(&queue->mutex);
    
    int next = (queue->tail + 1) % queue->capacity;
    if (next == queue->head) {
        pthread_mutex_unlock(&queue->mutex);
        return -1; // Queue full
    }
    
    queue->frames[queue->tail].data = malloc(size);
    if (!queue->frames[queue->tail].data) {
        pthread_mutex_unlock(&queue->mutex);
        return -1;
    }
    
    memcpy(queue->frames[queue->tail].data, data, size);
    queue->frames[queue->tail].size = size;
    queue->tail = next;
    queue->size++;
    
    pthread_mutex_unlock(&queue->mutex);
    return 0;
}

// Dequeue audio data
int dequeue_audio(audio_queue_t *queue, unsigned char *data, size_t *size) {
    pthread_mutex_lock(&queue->mutex);
    
    if (queue->head == queue->tail) {
        pthread_mutex_unlock(&queue->mutex);
        return -1; // Queue empty
    }
    
    *size = queue->frames[queue->head].size;
    memcpy(data, queue->frames[queue->head].data, *size);
    free(queue->frames[queue->head].data);
    queue->frames[queue->head].data = NULL;
    queue->head = (queue->head + 1) % queue->capacity;
    queue->size--;
    
    pthread_mutex_unlock(&queue->mutex);
    return 0;
}

// Microphone capture thread
void *mic_capture_thread(void *arg) {
    PaStream *stream;
    PaError err = Pa_OpenDefaultStream(&stream, 1, 0, paFloat32, INPUT_SR, IN_BLOCK, NULL, NULL);
    if (err != paNoError) {
        fprintf(stderr, "Failed to open microphone stream: %s\n", Pa_GetErrorText(err));
        return NULL;
    }
    
    err = Pa_StartStream(stream);
    if (err != paNoError) {
        fprintf(stderr, "Failed to start microphone stream: %s\n", Pa_GetErrorText(err));
        Pa_CloseStream(stream);
        return NULL;
    }
    
    printf("🎤 Microphone active - start speaking!\n");
    
    float buffer[IN_BLOCK];
    while (running) {
        err = Pa_ReadStream(stream, buffer, IN_BLOCK);
        if (err != paNoError) {
            fprintf(stderr, "Microphone read error: %s\n", Pa_GetErrorText(err));
            break;
        }
        
        // Convert float32 to int16 and encode as base64
        unsigned char pcm16[IN_BLOCK * 2];
        for (int i = 0; i < IN_BLOCK; i++) {
            int16_t sample = (int16_t)(buffer[i] * 32767.0f);
            pcm16[i * 2] = sample & 0xFF;
            pcm16[i * 2 + 1] = (sample >> 8) & 0xFF;
        }
        
        // Enqueue for WebSocket sending
        if (enqueue_audio(&mic_queue, pcm16, IN_BLOCK * 2) == 0) {
            // Create JSON message with base64 encoded audio
            char base64_audio[IN_BLOCK * 3]; // Base64 is ~1.33x larger
            base64_encode(pcm16, IN_BLOCK * 2, base64_audio);
            
            char json_msg[2048];
            snprintf(json_msg, sizeof(json_msg),
                "{\"realtimeInput\":{\"audio\":{\"data\":\"%s\",\"mimeType\":\"audio/pcm;rate=%d\"}}}",
                base64_audio, INPUT_SR);
            
            // Send via WebSocket
            if (send_websocket_frame(json_msg, strlen(json_msg)) > 0) {
                printf("Sent audio frame\n");
            }
        }
        
        usleep(FRAME_MS * 1000); // Sleep for frame duration
    }
    
    Pa_StopStream(stream);
    Pa_CloseStream(stream);
    return NULL;
}

// Speaker playback thread
void *speaker_playback_thread(void *arg) {
    PaStream *stream;
    PaError err = Pa_OpenDefaultStream(&stream, 0, 1, paInt16, OUTPUT_SR, OUT_BLOCK, NULL, NULL);
    if (err != paNoError) {
        fprintf(stderr, "Failed to open speaker stream: %s\n", Pa_GetErrorText(err));
        return NULL;
    }
    
    err = Pa_StartStream(stream);
    if (err != paNoError) {
        fprintf(stderr, "Failed to start speaker stream: %s\n", Pa_GetErrorText(err));
        Pa_CloseStream(stream);
        return NULL;
    }
    
    printf("🔊 Speaker active\n");
    
    unsigned char audio_data[BUFFER_SIZE];
    size_t data_size;
    int frame_count = 0;
    
    while (running) {
        if (dequeue_audio(&spk_queue, audio_data, &data_size) == 0) {
            frame_count++;
            printf("🔊 Playing audio frame #%d, %zu bytes\n", frame_count, data_size);
            
            err = Pa_WriteStream(stream, audio_data, data_size / 2);
            if (err != paNoError) {
                fprintf(stderr, "Speaker write error: %s\n", Pa_GetErrorText(err));
                break;
            }
        } else {
            usleep(10000); // 10ms sleep if no audio
        }
    }
    
    Pa_StopStream(stream);
    Pa_CloseStream(stream);
    return NULL;
}

// WebSocket receive thread
void *websocket_receive_thread(void *arg) {
    char buffer[BUFFER_SIZE];
    
    printf("🔍 WebSocket receive thread started\n");
    
    while (running) {
        int bytes = receive_websocket_frame(buffer, sizeof(buffer));
        if (bytes <= 0) {
            if (bytes == 0) {
                printf("WebSocket connection closed by server\n");
            } else {
                fprintf(stderr, "WebSocket receive error: %d\n", bytes);
                // Don't break on error, just continue trying
                usleep(100000); // 100ms delay before retry
                continue;
            }
            break;
        }
        
        buffer[bytes] = '\0';
        printf("📨 Received message (%d bytes): %s\n", bytes, buffer);
        
        // Parse JSON and extract audio data
        json_object *json = json_tokener_parse(buffer);
        if (json) {
            printf("✅ JSON parsed successfully\n");
            
            // Check for setupComplete message
            json_object *setup_complete;
            if (json_object_object_get_ex(json, "setupComplete", &setup_complete)) {
                printf("🎉 Setup complete! Gemini is ready for audio\n");
                setup_complete_received = 1;
                json_object_put(json);
                continue;
            }
            
            // Check for error messages
            json_object *error;
            if (json_object_object_get_ex(json, "error", &error)) {
                printf("❌ Gemini API error: %s\n", json_object_get_string(error));
                json_object_put(json);
                continue;
            }
            
            json_object *server_content, *model_turn, *parts, *inline_data, *audio_data;
            
            if (json_object_object_get_ex(json, "serverContent", &server_content)) {
                printf("🔊 Processing server content\n");
                
                if (json_object_object_get_ex(server_content, "modelTurn", &model_turn)) {
                    printf("🤖 Model turn received\n");
                    if (json_object_object_get_ex(model_turn, "parts", &parts)) {
                        int array_len = json_object_array_length(parts);
                        printf("📦 Found %d parts in model turn\n", array_len);
                        
                        for (int i = 0; i < array_len; i++) {
                            json_object *part = json_object_array_get_idx(parts, i);
                            if (json_object_object_get_ex(part, "inlineData", &inline_data)) {
                                if (json_object_object_get_ex(inline_data, "data", &audio_data)) {
                                    const char *base64_audio = json_object_get_string(audio_data);
                                    if (base64_audio) {
                                        printf("🎵 Audio data found, length: %zu\n", strlen(base64_audio));
                                        // Decode base64 and enqueue for speaker
                                        unsigned char decoded[BUFFER_SIZE];
                                        int decoded_len = base64_decode(base64_audio, decoded, sizeof(decoded));
                                        if (decoded_len > 0) {
                                            printf("🔊 Decoded audio: %d bytes, enqueueing for speaker\n", decoded_len);
                                            enqueue_audio(&spk_queue, decoded, decoded_len);
                                        } else {
                                            printf("⚠️ Failed to decode base64 audio\n");
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
                
                // Check for input/output transcriptions
                json_object *input_trans, *output_trans;
                if (json_object_object_get_ex(server_content, "inputTranscription", &input_trans)) {
                    json_object *text;
                    if (json_object_object_get_ex(input_trans, "text", &text)) {
                        printf("🎙️ You said: %s\n", json_object_get_string(text));
                    }
                }
                
                if (json_object_object_get_ex(server_content, "outputTranscription", &output_trans)) {
                    json_object *text;
                    if (json_object_object_get_ex(output_trans, "text", &text)) {
                        printf("🤖 Gemini said: %s\n", json_object_get_string(text));
                    }
                }
            } else {
                printf("📝 Message received but no serverContent found\n");
            }
            
            json_object_put(json);
        } else {
            printf("⚠️ Failed to parse JSON message\n");
        }
    }
    
    printf("🔍 WebSocket receive thread ending\n");
    return NULL;
}

// Signal handler
void signal_handler(int sig) {
    printf("\n👋 Shutting down...\n");
    running = 0;
}

// Cleanup function
void cleanup() {
    if (ssl) {
        SSL_shutdown(ssl);
        SSL_free(ssl);
    }
    if (sock >= 0) {
        close(sock);
    }
    EVP_cleanup();
    Pa_Terminate();
    cleanup_audio_queues();
}

int main() {
    signal(SIGINT, signal_handler);
    signal(SIGTERM, signal_handler);
    
    atexit(cleanup);
    
    // Initialize PortAudio
    PaError err = Pa_Initialize();
    if (err != paNoError) {
        fprintf(stderr, "Failed to initialize PortAudio: %s\n", Pa_GetErrorText(err));
        return 1;
    }
    
    // Initialize audio queues
    if (init_audio_queues() < 0) {
        fprintf(stderr, "Failed to initialize audio queues\n");
        return 1;
    }
    
    // Initialize SSL
    if (init_ssl() < 0) {
        fprintf(stderr, "Failed to initialize SSL\n");
        return 1;
    }
    
    // Connect to WebSocket
    if (connect_websocket() < 0) {
        fprintf(stderr, "Failed to connect to WebSocket\n");
        return 1;
    }
    
    // Send setup message
    char setup_msg[1024];
    snprintf(setup_msg, sizeof(setup_msg),
        "{\"setup\":{\"model\":\"%s\",\"generationConfig\":{\"responseModalities\":[\"AUDIO\"],\"speechConfig\":{\"voiceConfig\":{\"prebuiltVoiceConfig\":{\"voiceName\":\"%s\"}}}},\"inputAudioTranscription\":{},\"outputAudioTranscription\":{},\"systemInstruction\":{\"parts\":[{\"text\":\"You are a helpful assistant. Be concise and respond naturally in conversation. Only respond in complete sentences.\"}]}}}",
        MODEL, VOICE);
    
    printf("📤 Sending setup message:\n%s\n", setup_msg);
    
    int send_result = send_websocket_frame(setup_msg, strlen(setup_msg));
    if (send_result <= 0) {
        fprintf(stderr, "Failed to send setup message, SSL_write returned: %d\n", send_result);
        unsigned long ssl_error = SSL_get_error(ssl, send_result);
        fprintf(stderr, "SSL error: %lu\n", ssl_error);
        return 1;
    }
    
    printf("Setup message sent successfully, %d bytes\n", send_result);
    
    printf("✅ Setup message sent\n");
    
    // Wait for setupComplete message before starting audio threads
    printf("⏳ Waiting for setupComplete from Gemini...\n");
    
    // Create only the WebSocket receive thread first
    pthread_t websocket_thread;
    if (pthread_create(&websocket_thread, NULL, websocket_receive_thread, NULL) != 0) {
        fprintf(stderr, "Failed to create WebSocket thread\n");
        return 1;
    }
    
    // Wait for setupComplete (with timeout)
    int timeout = 30; // 30 seconds timeout
    
    while (!setup_complete_received && timeout > 0 && running) {
        sleep(1);
        timeout--;
        
        // Check if we received setupComplete from the WebSocket thread
        if (timeout % 5 == 0) {
            printf("⏳ Still waiting for setupComplete... (%d seconds left)\n", timeout);
        }
    }
    
    if (!setup_complete_received) {
        printf("⚠️ Setup timeout, proceeding anyway...\n");
    } else {
        printf("🎉 Setup complete received!\n");
    }
    
    // Send a test message to trigger Gemini's response (like the Python version)
    printf("🧪 Sending test message to trigger response...\n");
    char test_msg[512];
    snprintf(test_msg, sizeof(test_msg),
        "{\"realtimeInput\":{\"text\":\"Hello, can you hear me?\"}}");
    
    if (send_websocket_frame(test_msg, strlen(test_msg)) > 0) {
        printf("✅ Test message sent\n");
    } else {
        printf("⚠️ Failed to send test message\n");
    }
    
    // Now create the audio threads
    printf("🎵 Starting audio threads...\n");
    pthread_t mic_thread, speaker_thread;
    
    if (pthread_create(&mic_thread, NULL, mic_capture_thread, NULL) != 0) {
        fprintf(stderr, "Failed to create microphone thread\n");
        return 1;
    }
    
    if (pthread_create(&speaker_thread, NULL, speaker_playback_thread, NULL) != 0) {
        fprintf(stderr, "Failed to create speaker thread\n");
        return 1;
    }
    
    printf("🚀 All threads started. Press Ctrl+C to stop.\n");
    
    // Wait for threads to finish
    printf("⏳ Waiting for threads...\n");
    
    // Use a timeout approach to check if threads are still running
    while (running) {
        sleep(1);
        // Check if any critical threads have died
        // For now, just keep the main thread alive
    }
    
    printf("🔄 Shutting down threads...\n");
    
    // Signal threads to stop
    running = 0;
    
    // Wait a bit for threads to finish
    sleep(2);
    
    printf("✅ Shutdown complete\n");
    return 0;
}

