import asyncio
import websockets
import json
import base64
import signal
import sys
from datetime import datetime

async def test_websocket_timeout_idle():
    """Test websocket timeout when completely idle (no messages sent)"""
    uri = "ws://localhost:8000/ws"
    # uri = "wss://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/ws"
    
    try:
        print("🔌 Connecting to WebSocket...")
        async with websockets.connect(uri) as ws:
            print("✅ Connected to WebSocket")
            
            # Send a test message to establish connection
            test_msg = json.dumps({"text": "Hello, starting timeout test"})
            await ws.send(test_msg)
            print("📤 Sent initial test message")
            
            # Wait for any response
            await asyncio.sleep(1)
            
            print("\n⏸️  Now stopping all communication...")
            print("⏱️  Waiting for 30 seconds (testing timeout behavior)...")
            print("   Uvicorn default timeout is typically 20 seconds\n")
            
            # Connection state tracking
            connection_alive = True
            start_time = datetime.now()
            
            async def message_receiver():
                """Continuously listen for messages and pings from server"""
                nonlocal connection_alive
                while connection_alive:
                    try:
                        # Try to receive with a timeout
                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        elapsed = int((datetime.now() - start_time).total_seconds())
                        
                        # Try to parse as JSON
                        try:
                            data = json.loads(message)
                            print(f"\n📨 Message received at {elapsed}s: {data}")
                        except json.JSONDecodeError:
                            # Not JSON, might be raw data or ping
                            print(f"\n📨 Non-JSON received at {elapsed}s (length: {len(message)} bytes)")
                            
                    except asyncio.TimeoutError:
                        pass
                    except websockets.exceptions.ConnectionClosed as e:
                        elapsed = int((datetime.now() - start_time).total_seconds())
                        print(f"\n❌ Connection closed after {elapsed} seconds!")
                        print(f"   Close code: {e.code}, reason: {e.reason}")
                        connection_alive = False
                        return
                    except Exception as e:
                        elapsed = int((datetime.now() - start_time).total_seconds())
                        print(f"\n⚠️  Error in receiver at {elapsed}s: {e}")
            
            # Start the receiver task
            receiver_task = asyncio.create_task(message_receiver())
            
            # Don't send anything for 30 seconds
            for i in range(30):
                if not connection_alive:
                    break
                await asyncio.sleep(1)
                elapsed = int((datetime.now() - start_time).total_seconds())
                print(f"   Still idle... {elapsed}/30 seconds", end='\r')
            
            # Stop the receiver
            connection_alive = False
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass
            
            if connection_alive:
                print("\n✅ 30 seconds passed - connection still alive!")
                
                # Try to send a message to confirm connection is still working
                try:
                    print("📤 Sending a final message to test connection...")
                    await ws.send(json.dumps({"text": "Are you still there?"}))
                    await asyncio.sleep(1)
                    print("✅ Connection is still active after timeout period!")
                except websockets.exceptions.ConnectionClosed as e:
                    print(f"❌ Connection closed when trying to send: {e}")
                
    except websockets.exceptions.ConnectionRefused:
        print("❌ Connection refused. Make sure the FastAPI server is running on localhost:8000")
    except Exception as e:
        print(f"❌ Error in test: {e}")
        import traceback
        traceback.print_exc()


async def test_websocket_timeout_with_silence():
    """Test websocket timeout when sending silent audio (no real sound)"""
    uri = "ws://localhost:8000/ws"
    # uri = "wss://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/ws"
    
    try:
        print("🔌 Connecting to WebSocket...")
        async with websockets.connect(uri) as ws:
            print("✅ Connected to WebSocket")
            
            # Send silent audio continuously to keep connection alive
            silent_chunk = b'\x00' * 1024  # Silent audio data (zeros)
            
            print("📤 Sending silent audio to keep connection alive...")
            print("⏱️  After 5 seconds, will stop sending for 30 seconds...\n")
            
            # Send silent audio for 5 seconds
            for i in range(50):  # 50 * 0.1s = 5 seconds
                await asyncio.sleep(0.1)
                msg = {
                    "audio": base64.b64encode(silent_chunk).decode("utf-8")
                }
                await ws.send(json.dumps(msg))
                if i % 10 == 0:
                    print(f"   Sending silent audio... {int(i/10)}/5 seconds", end='\r')
            
            print("\n\n⏸️  Stopped sending audio. Now waiting 30 seconds...")
            print("   Testing if connection closes without any data...\n")
            
            # Connection state tracking
            connection_alive = True
            start_time = datetime.now()
            
            async def message_receiver():
                """Continuously listen for messages and pings from server"""
                nonlocal connection_alive
                while connection_alive:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        elapsed = int((datetime.now() - start_time).total_seconds())
                        
                        try:
                            data = json.loads(message)
                            print(f"\n📨 Message received at {elapsed}s: {data}")
                        except json.JSONDecodeError:
                            print(f"\n📨 Non-JSON received at {elapsed}s (length: {len(message)} bytes)")
                            
                    except asyncio.TimeoutError:
                        pass
                    except websockets.exceptions.ConnectionClosed as e:
                        elapsed = int((datetime.now() - start_time).total_seconds())
                        print(f"\n❌ Connection closed after {elapsed} seconds idle (no audio)")
                        print(f"   Close code: {e.code}, reason: {e.reason}")
                        connection_alive = False
                        return
                    except Exception as e:
                        print(f"\n⚠️  Error: {e}")
            
            receiver_task = asyncio.create_task(message_receiver())
            
            # Now stop sending and wait
            for i in range(30):
                if not connection_alive:
                    break
                await asyncio.sleep(1)
                elapsed = int((datetime.now() - start_time).total_seconds())
                print(f"   Still idle after silence... {elapsed}/30 seconds", end='\r')
            
            connection_alive = False
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass
            
            if connection_alive:
                print("\n✅ Connection remained active during 30 seconds of silence!")
                print("   The server may have websocket pings enabled or a longer timeout.")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


async def test_websocket_timeout_immediate():
    """Test websocket timeout immediately after connection without sending anything"""
    uri = "ws://localhost:8000/ws"
    # uri = "wss://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io/ws"
    
    try:
        print("🔌 Connecting to WebSocket...")
        async with websockets.connect(uri) as ws:
            print("✅ Connected to WebSocket")
            print("⏸️  Not sending any initial message...")
            print("⏱️  Waiting for 30 seconds without sending anything...\n")
            
            # Connection state tracking
            connection_alive = True
            start_time = datetime.now()
            
            async def message_receiver():
                """Continuously listen for messages and pings from server"""
                nonlocal connection_alive
                while connection_alive:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        elapsed = int((datetime.now() - start_time).total_seconds())
                        
                        try:
                            data = json.loads(message)
                            print(f"\n📨 Message received at {elapsed}s: {data}")
                        except json.JSONDecodeError:
                            print(f"\n📨 Non-JSON received at {elapsed}s (length: {len(message)} bytes)")
                            
                    except asyncio.TimeoutError:
                        pass
                    except websockets.exceptions.ConnectionClosed as e:
                        elapsed = int((datetime.now() - start_time).total_seconds())
                        print(f"\n❌ Connection closed after {elapsed} seconds")
                        print(f"   Close code: {e.code}, reason: {e.reason}")
                        print(f"   Server closed connection due to inactivity")
                        connection_alive = False
                        return
                    except Exception as e:
                        print(f"\n⚠️  Error: {e}")
            
            receiver_task = asyncio.create_task(message_receiver())
            
            for i in range(30):
                if not connection_alive:
                    break
                await asyncio.sleep(1)
                elapsed = int((datetime.now() - start_time).total_seconds())
                print(f"   Still idle... {elapsed}/30 seconds", end='\r')
            
            connection_alive = False
            receiver_task.cancel()
            try:
                await receiver_task
            except asyncio.CancelledError:
                pass
            
            if connection_alive:
                print("\n✅ 30 seconds passed - connection still alive!")
            
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


def signal_handler(sig, frame):
    print("\n\n🛑 Shutting down gracefully...")
    sys.exit(0)


if __name__ == "__main__":
    # Set up signal handler for graceful shutdown
    signal.signal(signal.SIGINT, signal_handler)
    
    import sys
    
    # Check which test to run
    if len(sys.argv) > 1:
        test_name = sys.argv[1]
        
        if test_name == "idle":
            print("🧪 Running WebSocket timeout test (completely idle)...\n")
            asyncio.run(test_websocket_timeout_idle())
        elif test_name == "silence":
            print("🧪 Running WebSocket timeout test (with silent audio)...\n")
            asyncio.run(test_websocket_timeout_with_silence())
        elif test_name == "immediate":
            print("🧪 Running WebSocket timeout test (immediate, no messages)...\n")
            asyncio.run(test_websocket_timeout_immediate())
        else:
            print(f"Unknown test: {test_name}")
            print("\nAvailable tests:")
            print("  - idle      : Connect, send one message, then go idle")
            print("  - silence   : Send silent audio for 5s, then stop for 30s")
            print("  - immediate : Connect and immediately go idle without sending anything")
    else:
        print("Usage: python test_websocket_timeout.py [test_name]")
        print("\nAvailable tests:")
        print("  - idle      : Connect, send one message, then go idle")
        print("  - silence   : Send silent audio for 5s, then stop for 30s")
        print("  - immediate : Connect and immediately go idle without sending anything")
        print("\nExample: python test_websocket_timeout.py silence")
        