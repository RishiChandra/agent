import 'dart:convert';
import 'package:http/http.dart' as http;

class Task {
  final String taskId;
  final String userId;
  final Map<String, dynamic>? taskInfo;
  final String? status;
  final DateTime? timeToExecute;

  Task({required this.taskId, required this.userId, this.taskInfo, this.status, this.timeToExecute});

  Map<String, dynamic> toMap() {
    return {'task_id': taskId, 'user_id': userId, 'task_info': taskInfo, 'status': status, 'time_to_execute': timeToExecute?.toIso8601String()};
  }

  factory Task.fromMap(Map<String, dynamic> map) {
    return Task(taskId: map['task_id'] as String, userId: map['user_id'] as String, taskInfo: map['task_info'] as Map<String, dynamic>?, status: map['status'] as String?, timeToExecute: map['time_to_execute'] != null ? DateTime.parse(map['time_to_execute'] as String) : null);
  }
}

class TaskService {
  // Backend API base URL
  static const String _backendBaseUrl = 'https://websocket-ai-pin.bluesmoke-32dd7ab8.westus2.azurecontainerapps.io';

  // Helper method to handle HTTP errors
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

  // Get all tasks for a user
  Future<List<Task>> getTasksByUserId(String userId) async {
    try {
      final url = Uri.parse('$_backendBaseUrl/tasks/$userId');
      final response = await http.get(url);

      if (response.statusCode != 200) {
        _handleHttpError(response, 'Fetching tasks');
      }

      final data = jsonDecode(response.body) as Map<String, dynamic>;
      final tasksList = data['tasks'] as List<dynamic>;

      return tasksList.map((taskMap) {
        return Task.fromMap(taskMap as Map<String, dynamic>);
      }).toList();
    } catch (e) {
      throw 'Error fetching tasks: $e';
    }
  }

  // Get a single task by ID
  Future<Task?> getTaskById(String taskId, String userId) async {
    try {
      final url = Uri.parse('$_backendBaseUrl/tasks/$userId/$taskId');
      final response = await http.get(url);

      if (response.statusCode == 404) {
        return null;
      }

      if (response.statusCode != 200) {
        _handleHttpError(response, 'Fetching task');
      }

      final taskMap = jsonDecode(response.body) as Map<String, dynamic>;
      return Task.fromMap(taskMap);
    } catch (e) {
      throw 'Error fetching task: $e';
    }
  }

  // Create a new task
  Future<Task> createTask({
    required String userId,
    Map<String, dynamic>? taskInfo,
    String? status,
    DateTime? timeToExecute,
    bool enqueue = true, // Whether to enqueue to Service Bus (default: true)
  }) async {
    try {
      final url = Uri.parse('$_backendBaseUrl/tasks');

      final requestBody = <String, dynamic>{
        'user_id': userId,
        if (taskInfo != null) 'task_info': taskInfo,
        if (status != null) 'status': status,
        if (timeToExecute != null) 'time_to_execute': timeToExecute.toIso8601String(),
        if (timeToExecute != null) 'timezone': timeToExecute.timeZoneName, // Send timezone name
        if (timeToExecute != null) 'timezone_offset': timeToExecute.timeZoneOffset.inSeconds / 3600.0, // Send timezone offset in hours (as double to support fractional hours)
        'enqueue': enqueue,
      };

      final response = await http.post(url, headers: {'Content-Type': 'application/json'}, body: jsonEncode(requestBody));

      if (response.statusCode != 200) {
        _handleHttpError(response, 'Creating task');
      }

      final taskMap = jsonDecode(response.body) as Map<String, dynamic>;
      return Task.fromMap(taskMap);
    } catch (e) {
      throw 'Error creating task: $e';
    }
  }

  // Update an existing task
  Future<Task> updateTask({required String taskId, required String userId, Map<String, dynamic>? taskInfo, String? status, DateTime? timeToExecute}) async {
    try {
      final url = Uri.parse('$_backendBaseUrl/tasks/$userId/$taskId');

      final requestBody = <String, dynamic>{};
      if (taskInfo != null) {
        requestBody['task_info'] = taskInfo;
      }
      if (status != null) {
        requestBody['status'] = status;
      }
      if (timeToExecute != null) {
        requestBody['time_to_execute'] = timeToExecute.toIso8601String();
        requestBody['timezone'] = timeToExecute.timeZoneName; // Send timezone name
        requestBody['timezone_offset'] = timeToExecute.timeZoneOffset.inSeconds / 3600.0; // Send timezone offset in hours (as double to support fractional hours)
      }

      final response = await http.put(url, headers: {'Content-Type': 'application/json'}, body: jsonEncode(requestBody));

      if (response.statusCode != 200) {
        _handleHttpError(response, 'Updating task');
      }

      final taskMap = jsonDecode(response.body) as Map<String, dynamic>;
      return Task.fromMap(taskMap);
    } catch (e) {
      throw 'Error updating task: $e';
    }
  }

  // Delete a task
  Future<void> deleteTask(String taskId, String userId) async {
    try {
      final url = Uri.parse('$_backendBaseUrl/tasks/$userId/$taskId');
      final response = await http.delete(url);

      if (response.statusCode != 200) {
        _handleHttpError(response, 'Deleting task');
      }
    } catch (e) {
      throw 'Error deleting task: $e';
    }
  }
}
