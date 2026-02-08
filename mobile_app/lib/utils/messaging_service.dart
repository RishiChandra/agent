import 'dart:convert';
import 'package:http/http.dart' as http;

class MessagingService {
  static const String _backendBaseUrl = 'https://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io';

  void _handleHttpError(http.Response response, String operation) {
    if (response.statusCode >= 400) {
      try {
        final errorBody = jsonDecode(response.body) as Map<String, dynamic>;
        final detail = errorBody['detail'] ?? 'Unknown error';
        throw '$operation failed: $detail';
      } catch (e) {
        throw '$operation failed: ${response.statusCode} - ${response.body}';
      }
    }
  }

  /// Triggers the message enqueue endpoint so the AI will respond in the chip (one pending per user).
  Future<void> enqueueMessage({required String userId, required String chatId}) async {
    final url = Uri.parse('$_backendBaseUrl/messages/enqueue');
    final requestBody = <String, dynamic>{'user_id': userId, 'chat_id': chatId};
    final response = await http.post(url, headers: {'Content-Type': 'application/json'}, body: jsonEncode(requestBody));
    if (response.statusCode >= 400) {
      _handleHttpError(response, 'Enqueue message');
    }
  }

  /// Sends a message to the backend. Returns the created message (including message_id).
  Future<Map<String, dynamic>> sendMessage({required String userId, required String chatId, required String content, required DateTime timestamp}) async {
    final url = Uri.parse('$_backendBaseUrl/messages');
    final requestBody = <String, dynamic>{'user_id': userId, 'chat_id': chatId, 'content': content, 'timestamp': timestamp.toUtc().toIso8601String()};

    final response = await http.post(url, headers: {'Content-Type': 'application/json'}, body: jsonEncode(requestBody));

    if (response.statusCode != 200) {
      _handleHttpError(response, 'Sending message');
    }

    return jsonDecode(response.body) as Map<String, dynamic>;
  }

  /// Fetches chat history for a chat, sorted by timestamp ascending.
  /// Returns list of messages with message_id, sender_id, content, created_at.
  Future<List<Map<String, dynamic>>> getChatHistory({required String chatId}) async {
    final url = Uri.parse('$_backendBaseUrl/messages').replace(queryParameters: {'chat_id': chatId});

    final response = await http.get(url);

    if (response.statusCode >= 400) {
      _handleHttpError(response, 'Fetching chat history');
    }

    final body = jsonDecode(response.body) as Map<String, dynamic>;
    final list = body['messages'] as List<dynamic>? ?? [];
    return list.map((e) => Map<String, dynamic>.from(e as Map)).toList();
  }
}
