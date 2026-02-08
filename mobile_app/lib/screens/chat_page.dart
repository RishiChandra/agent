import 'package:flutter/material.dart';
import '../utils/design_system.dart';
import '../utils/messaging_service.dart';

/// Fixed chat ID used for now (can be overridden via [chatId]).
const String kDefaultChatId = '550e8400-e29b-41d4-a716-446655440000';

class ChatPage extends StatefulWidget {
  /// User ID from profile (sign in, sign up, or splash screen).
  final String userId;

  /// If null, uses [kDefaultChatId].
  final String? chatId;

  const ChatPage({super.key, required this.userId, this.chatId});

  @override
  State<ChatPage> createState() => _ChatPageState();
}

class _ChatPageState extends State<ChatPage> {
  final TextEditingController _messageController = TextEditingController();
  final MessagingService _messagingService = MessagingService();
  bool _sending = false;
  bool _loading = true;
  List<Map<String, dynamic>> _messages = [];

  String get _effectiveChatId => widget.chatId ?? kDefaultChatId;

  @override
  void initState() {
    super.initState();
    _loadChatHistory();
  }

  @override
  void dispose() {
    _messageController.dispose();
    super.dispose();
  }

  Future<void> _loadChatHistory({bool silent = false}) async {
    if (!silent) setState(() => _loading = true);
    try {
      final list = await _messagingService.getChatHistory(chatId: _effectiveChatId);
      list.sort((a, b) {
        final ta = _parseTimestamp(a['created_at']);
        final tb = _parseTimestamp(b['created_at']);
        return ta.compareTo(tb);
      });
      if (!mounted) return;
      setState(() {
        _messages = list;
        _loading = false;
      });
    } catch (e) {
      if (!mounted) return;
      setState(() => _loading = false);
      if (!silent) ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Failed to load chat: $e')));
    }
  }

  DateTime _parseTimestamp(dynamic v) {
    if (v == null) return DateTime.now();
    if (v is DateTime) return v;
    return DateTime.tryParse(v.toString()) ?? DateTime.now();
  }

  String _formatTime(DateTime dt) {
    final now = DateTime.now();
    final today = DateTime(now.year, now.month, now.day);
    final msgDay = DateTime(dt.year, dt.month, dt.day);
    final hour = dt.hour > 12 ? dt.hour - 12 : (dt.hour == 0 ? 12 : dt.hour);
    final min = dt.minute.toString().padLeft(2, '0');
    final ampm = dt.hour >= 12 ? 'PM' : 'AM';
    if (msgDay == today) {
      return '$hour:$min $ampm';
    }
    return '${dt.month}/${dt.day} $hour:$min $ampm';
  }

  Future<void> _sendMessage() async {
    final text = _messageController.text.trim();
    if (text.isEmpty || _sending) return;

    setState(() => _sending = true);
    _messageController.clear();

    try {
      // POST /messages saves the message and enqueues for the AI; no need to call /messages/enqueue separately
      await _messagingService.sendMessage(userId: widget.userId, chatId: _effectiveChatId, content: text, timestamp: DateTime.now());
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Message sent')));
      await _loadChatHistory(silent: true);
    } catch (e) {
      if (!mounted) return;
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Failed to send: $e')));
      // Put text back so user can retry
      _messageController.text = text;
    } finally {
      if (mounted) setState(() => _sending = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(leading: IconButton(icon: const Icon(Icons.arrow_back), onPressed: () => Navigator.of(context).pop(), tooltip: 'Back'), title: const Text('Chat'), scrolledUnderElevation: 0, surfaceTintColor: Colors.transparent),
      body: SafeArea(
        child: Column(
          children: [
            Expanded(
              child:
                  _loading
                      ? const Center(child: CircularProgressIndicator())
                      : ListView.builder(
                        padding: const EdgeInsets.symmetric(horizontal: AppSpacing.md, vertical: AppSpacing.sm),
                        itemCount: _messages.length,
                        itemBuilder: (context, index) {
                          final msg = _messages[index];
                          final content = msg['content'] as String? ?? '';
                          final createdAt = _parseTimestamp(msg['created_at']);
                          final isMe = msg['sender_id'] == widget.userId;
                          return Padding(padding: const EdgeInsets.only(bottom: AppSpacing.sm), child: Align(alignment: isMe ? Alignment.centerRight : Alignment.centerLeft, child: Container(padding: const EdgeInsets.symmetric(horizontal: AppSpacing.md, vertical: AppSpacing.sm), decoration: BoxDecoration(color: isMe ? Theme.of(context).colorScheme.primaryContainer : Colors.grey[200], borderRadius: BorderRadius.circular(AppSizes.radiusMedium)), constraints: BoxConstraints(maxWidth: MediaQuery.sizeOf(context).width * 0.75), child: Column(crossAxisAlignment: CrossAxisAlignment.start, mainAxisSize: MainAxisSize.min, children: [Text(content, style: AppTextStyles.body(context)), const SizedBox(height: AppSpacing.xs), Text(_formatTime(createdAt), style: AppTextStyles.caption(context).copyWith(color: Colors.grey[600], fontSize: 11))]))));
                        },
                      ),
            ),
            Padding(
              padding: const EdgeInsets.fromLTRB(AppSpacing.md, AppSpacing.sm, AppSpacing.md, AppSpacing.md),
              child: Row(crossAxisAlignment: CrossAxisAlignment.end, children: [Expanded(child: TextField(controller: _messageController, decoration: InputDecoration(hintText: 'Type a message...', border: OutlineInputBorder(borderRadius: BorderRadius.circular(AppSizes.radiusLarge)), filled: true, fillColor: Colors.grey[50], contentPadding: const EdgeInsets.symmetric(horizontal: AppSpacing.md, vertical: AppSpacing.sm)), maxLines: null, textInputAction: TextInputAction.send, onSubmitted: (_) => _sendMessage(), enabled: !_sending)), const SizedBox(width: AppSpacing.sm), IconButton.filled(onPressed: _sending ? null : _sendMessage, icon: _sending ? SizedBox(width: 20, height: 20, child: CircularProgressIndicator(strokeWidth: 2, color: Theme.of(context).colorScheme.onPrimary)) : const Icon(Icons.send), tooltip: _sending ? 'Sending...' : 'Send', style: IconButton.styleFrom(backgroundColor: Theme.of(context).colorScheme.primary, foregroundColor: Theme.of(context).colorScheme.onPrimary))]),
            ),
          ],
        ),
      ),
    );
  }
}
