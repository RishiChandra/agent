import 'package:flutter/material.dart';
import 'package:intl/intl.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../utils/task_service.dart';
import '../utils/design_system.dart';
import '../utils/widgets/app_button.dart';
import '../utils/widgets/app_error_message.dart';
import '../backend/auth_service.dart';
import 'splash_screen.dart';
import 'esp_prov_page.dart';

class TasksPage extends StatefulWidget {
  final String userId;

  const TasksPage({super.key, required this.userId});

  @override
  State<TasksPage> createState() => _TasksPageState();
}

class _TasksPageState extends State<TasksPage> {
  final TaskService _taskService = TaskService();
  final AuthService _authService = AuthService();
  List<Task> _tasks = [];
  bool _isLoading = true;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    _loadTasks();
  }

  Future<void> _loadTasks() async {
    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    try {
      final tasks = await _taskService.getTasksByUserId(widget.userId);
      setState(() {
        _tasks = tasks;
        _isLoading = false;
      });
    } catch (e) {
      setState(() {
        _errorMessage = e.toString();
        _isLoading = false;
      });
    }
  }

  Future<void> _handleDeleteTask(String taskId) async {
    final confirmed = await showDialog<bool>(context: context, builder: (context) => AlertDialog(title: const Text('Delete Task'), content: const Text('Are you sure you want to delete this task?'), actions: [TextButton(onPressed: () => Navigator.of(context).pop(false), child: const Text('Cancel')), TextButton(onPressed: () => Navigator.of(context).pop(true), style: TextButton.styleFrom(foregroundColor: Colors.red), child: const Text('Delete'))]));

    if (confirmed != true) return;

    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    try {
      await _taskService.deleteTask(taskId, widget.userId);
      await _loadTasks();
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Task deleted successfully'), backgroundColor: Colors.green));
      }
    } catch (e) {
      setState(() {
        _errorMessage = e.toString();
        _isLoading = false;
      });
    }
  }

  Future<void> _handleSignOut() async {
    try {
      await _authService.signOut();
      // Clear local storage
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('is_signed_in', false);
      await prefs.remove('user_id');

      if (mounted) {
        Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const SplashScreen()), (route) => false);
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error signing out: $e'), backgroundColor: Colors.red));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(
        title: const Text('Tasks'),
        scrolledUnderElevation: 0,
        surfaceTintColor: Colors.transparent,
        actions: [
          IconButton(
            icon: const Icon(Icons.bluetooth),
            onPressed: () {
              Navigator.of(context).push(MaterialPageRoute(builder: (_) => const EspProvPage()));
            },
            tooltip: 'Link Hardware',
          ),
          IconButton(icon: const Icon(Icons.logout), onPressed: _handleSignOut, tooltip: 'Sign Out'),
        ],
      ),
      body: SafeArea(
        child: Column(
          children: [
            if (_errorMessage != null) Padding(padding: const EdgeInsets.all(AppSpacing.md), child: AppErrorMessage(message: _errorMessage!)),
            Expanded(
              child:
                  _isLoading
                      ? const Center(child: CircularProgressIndicator())
                      : _tasks.isEmpty
                      ? Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [Icon(Icons.task_alt, size: AppSizes.iconXLarge, color: Colors.grey[400]), const SizedBox(height: AppSpacing.md), Text('No tasks yet', style: AppTextStyles.subheading(context).copyWith(color: Colors.grey[600])), const SizedBox(height: AppSpacing.sm), Text('Tap the + button to create your first task', style: AppTextStyles.bodySmall(context))]))
                      : RefreshIndicator(
                        onRefresh: _loadTasks,
                        child: ListView.builder(
                          padding: const EdgeInsets.all(AppSpacing.md),
                          itemCount: _tasks.length,
                          itemBuilder: (context, index) {
                            final task = _tasks[index];
                            return _TaskCard(task: task, onEdit: () => _showTaskDialog(task: task), onDelete: () => _handleDeleteTask(task.taskId));
                          },
                        ),
                      ),
            ),
          ],
        ),
      ),
      floatingActionButton: FloatingActionButton(onPressed: () => _showTaskDialog(), child: const Icon(Icons.add)),
    );
  }

  Future<void> _showTaskDialog({Task? task}) async {
    final result = await showDialog<Map<String, dynamic>>(context: context, builder: (context) => _TaskDialog(userId: widget.userId, task: task, taskService: _taskService));

    if (result != null && mounted) {
      await _loadTasks();
      ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(task == null ? 'Task created successfully' : 'Task updated successfully'), backgroundColor: Colors.green));
    }
  }
}

class _TaskCard extends StatelessWidget {
  final Task task;
  final VoidCallback onEdit;
  final VoidCallback onDelete;

  const _TaskCard({required this.task, required this.onEdit, required this.onDelete});

  @override
  Widget build(BuildContext context) {
    return Card(
      margin: const EdgeInsets.only(bottom: AppSpacing.md),
      child: InkWell(
        onTap: onEdit,
        borderRadius: BorderRadius.circular(AppSizes.radiusMedium),
        child: Padding(
          padding: const EdgeInsets.all(AppSpacing.md),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(children: [Expanded(child: Text(task.status ?? 'No status', style: AppTextStyles.body(context).copyWith(fontWeight: FontWeight.bold, color: _getStatusColor(task.status)))), IconButton(icon: const Icon(Icons.edit, size: AppSizes.iconSmall), onPressed: onEdit, tooltip: 'Edit'), IconButton(icon: const Icon(Icons.delete, size: AppSizes.iconSmall), onPressed: onDelete, tooltip: 'Delete', color: Colors.red)]),
              if (task.taskInfo != null && task.taskInfo!.isNotEmpty) ...[const SizedBox(height: AppSpacing.sm), ...task.taskInfo!.entries.map((entry) => Padding(padding: const EdgeInsets.only(bottom: AppSpacing.xs), child: Text('${entry.key}: ${entry.value}', style: AppTextStyles.bodySmall(context))))],
              if (task.timeToExecute != null) ...[
                const SizedBox(height: AppSpacing.sm),
                Row(children: [Icon(Icons.schedule, size: AppSizes.iconSmall, color: Colors.grey[600]), const SizedBox(width: AppSpacing.xs), Text(DateFormat('MMM d, y • h:mm a').format(task.timeToExecute!.toLocal()), style: AppTextStyles.bodySmall(context))]),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Color _getStatusColor(String? status) {
    switch (status?.toLowerCase()) {
      case 'completed':
      case 'done':
      case 'finished':
        return Colors.green;
      case 'in progress':
      case 'pending':
        return Colors.orange;
      case 'cancelled':
        return Colors.red;
      default:
        return Colors.grey;
    }
  }
}

class _TaskDialog extends StatefulWidget {
  final String userId;
  final Task? task;
  final TaskService taskService;

  const _TaskDialog({required this.userId, this.task, required this.taskService});

  @override
  State<_TaskDialog> createState() => _TaskDialogState();
}

class _TaskDialogState extends State<_TaskDialog> {
  final _formKey = GlobalKey<FormState>();
  final _taskInfoController = TextEditingController();
  String _selectedStatus = 'pending';
  DateTime? _selectedDateTime;
  bool _isLoading = false;
  String? _errorMessage;

  @override
  void initState() {
    super.initState();
    if (widget.task != null) {
      _selectedStatus = widget.task!.status ?? 'pending';
      // Extract the "info" value from task_info JSON, or use empty string
      if (widget.task!.taskInfo != null && widget.task!.taskInfo!['info'] != null) {
        _taskInfoController.text = widget.task!.taskInfo!['info'].toString();
      }
      _selectedDateTime = widget.task!.timeToExecute;
    }
  }

  @override
  void dispose() {
    _taskInfoController.dispose();
    super.dispose();
  }

  Future<void> _selectDateTime() async {
    final date = await showDatePicker(context: context, initialDate: _selectedDateTime ?? DateTime.now(), firstDate: DateTime.now(), lastDate: DateTime.now().add(const Duration(days: 365)));

    if (date == null) return;

    final time = await showTimePicker(context: context, initialTime: _selectedDateTime != null ? TimeOfDay.fromDateTime(_selectedDateTime!) : TimeOfDay.now());

    if (time != null) {
      setState(() {
        // Create DateTime in local timezone (preserves user's selected time)
        _selectedDateTime = DateTime(date.year, date.month, date.day, time.hour, time.minute);
      });
    }
  }

  Map<String, dynamic>? _parseTaskInfo() {
    final text = _taskInfoController.text.trim();
    if (text.isEmpty) {
      return null;
    }

    // Convert user input to JSON format: {"info": "user input"}
    return {'info': text};
  }

  Future<void> _handleSave() async {
    if (!_formKey.currentState!.validate()) {
      return;
    }

    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    try {
      final taskInfo = _parseTaskInfo();

      if (widget.task == null) {
        // Create new task - status defaults to "pending"
        await widget.taskService.createTask(userId: widget.userId, taskInfo: taskInfo, status: _selectedStatus, timeToExecute: _selectedDateTime);
      } else {
        // Update existing task
        await widget.taskService.updateTask(taskId: widget.task!.taskId, userId: widget.userId, taskInfo: taskInfo, status: _selectedStatus, timeToExecute: _selectedDateTime);
      }

      if (mounted) {
        Navigator.of(context).pop({'success': true});
      }
    } catch (e) {
      setState(() {
        _errorMessage = e.toString();
        _isLoading = false;
      });
    }
  }

  @override
  Widget build(BuildContext context) {
    return Dialog(
      child: Container(
        constraints: const BoxConstraints(maxWidth: 500, maxHeight: 700),
        child: Form(
          key: _formKey,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              AppBar(title: Text(widget.task == null ? 'Create Task' : 'Edit Task'), automaticallyImplyLeading: false, actions: [IconButton(icon: const Icon(Icons.close), onPressed: () => Navigator.of(context).pop())]),
              Expanded(
                child: SingleChildScrollView(
                  padding: const EdgeInsets.all(AppSpacing.lg),
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      if (_errorMessage != null) ...[AppErrorMessage(message: _errorMessage!), const SizedBox(height: AppSpacing.md)],
                      DropdownButtonFormField<String>(
                        value: _selectedStatus,
                        decoration: InputDecoration(labelText: 'Status', prefixIcon: const Icon(Icons.info_outline), border: OutlineInputBorder(borderRadius: BorderRadius.circular(AppSizes.radiusMedium)), filled: true, fillColor: Colors.grey[50]),
                        items: const [DropdownMenuItem(value: 'pending', child: Text('Pending')), DropdownMenuItem(value: 'finished', child: Text('Finished'))],
                        onChanged:
                            widget.task == null
                                ? null // Disable when creating new task
                                : (value) {
                                  if (value != null) {
                                    setState(() {
                                      _selectedStatus = value;
                                    });
                                  }
                                },
                      ),
                      const SizedBox(height: AppSpacing.md),
                      TextFormField(controller: _taskInfoController, decoration: InputDecoration(labelText: 'Task Info', hintText: 'Brush my teeth', prefixIcon: const Icon(Icons.text_fields), border: OutlineInputBorder(borderRadius: BorderRadius.circular(AppSizes.radiusMedium)), filled: true, fillColor: Colors.grey[50]), maxLines: 3),
                      const SizedBox(height: AppSpacing.md),
                      OutlinedButton.icon(onPressed: _selectDateTime, icon: const Icon(Icons.calendar_today), label: Text(_selectedDateTime != null ? DateFormat('MMM d, y • h:mm a').format(_selectedDateTime!.toLocal()) : 'Select Date & Time')),
                      if (_selectedDateTime != null) ...[
                        const SizedBox(height: AppSpacing.sm),
                        TextButton(
                          onPressed: () {
                            setState(() {
                              _selectedDateTime = null;
                            });
                          },
                          child: const Text('Clear Date & Time'),
                        ),
                      ],
                    ],
                  ),
                ),
              ),
              Padding(padding: const EdgeInsets.all(AppSpacing.md), child: AppPrimaryButton(text: widget.task == null ? 'Create' : 'Update', onPressed: _isLoading ? null : _handleSave, isLoading: _isLoading)),
            ],
          ),
        ),
      ),
    );
  }
}
