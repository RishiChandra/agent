CC = gcc
CFLAGS = -Wall -Wextra -std=c99 -O2 -g
LDFLAGS = -lpthread -lssl -lcrypto -lportaudio -ljson-c

# On macOS, you might need to add these paths
ifeq ($(shell uname), Darwin)
    CFLAGS += -I/opt/homebrew/include
    LDFLAGS += -L/opt/homebrew/lib
endif

TARGET = gemini_live_c
SIMPLE_TARGET = gemini_live_c_simple
SOURCE = gemini_live_c.c
SIMPLE_SOURCE = gemini_live_c_simple.c

.PHONY: all clean install-deps simple

all: $(TARGET)

simple: $(SIMPLE_TARGET)

$(TARGET): $(SOURCE)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

$(SIMPLE_TARGET): $(SIMPLE_SOURCE)
	$(CC) $(CFLAGS) -o $@ $< $(LDFLAGS)

clean:
	rm -f $(TARGET) $(SIMPLE_TARGET)

install-deps:
	@echo "Installing dependencies..."
ifeq ($(shell uname), Darwin)
	@echo "Installing on macOS..."
	brew install portaudio openssl json-c
else
	@echo "Installing on Linux..."
	sudo apt-get update
	sudo apt-get install -y libportaudio2 libportaudio-dev libssl-dev libjson-c-dev
endif

run: $(TARGET)
	@echo "Make sure GOOGLE_API_KEY environment variable is set"
	./$(TARGET)

run-simple: $(SIMPLE_TARGET)
	@echo "Make sure GOOGLE_API_KEY environment variable is set"
	./$(SIMPLE_TARGET)

test-build: clean all simple
	@echo "✅ Both versions built successfully"
	@echo "Use 'make run' for the full version or 'make run-simple' for the simplified version"

help:
	@echo "Available targets:"
	@echo "  all          - Build the full version (requires base64.h)"
	@echo "  simple       - Build the simplified version (includes base64 functions)"
	@echo "  clean        - Remove built files"
	@echo "  install-deps - Install system dependencies"
	@echo "  run          - Build and run the full version"
	@echo "  run-simple   - Build and run the simplified version"
	@echo "  test-build   - Build both versions to test compilation"
	@echo "  help         - Show this help message"
	@echo ""
	@echo "Recommended: Use 'make simple' for easier compilation"
