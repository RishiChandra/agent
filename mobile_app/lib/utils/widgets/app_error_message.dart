import 'package:flutter/material.dart';
import '../design_system.dart';

/// Standardized error message display
class AppErrorMessage extends StatelessWidget {
  final String message;

  const AppErrorMessage({super.key, required this.message});

  @override
  Widget build(BuildContext context) {
    return Container(padding: const EdgeInsets.all(AppSpacing.md), margin: const EdgeInsets.only(bottom: AppSpacing.md), decoration: BoxDecoration(color: Colors.red[50], borderRadius: BorderRadius.circular(AppSizes.radiusSmall), border: Border.all(color: Colors.red[200]!)), child: Row(children: [Icon(Icons.error_outline, color: Colors.red[700]), const SizedBox(width: AppSpacing.sm), Expanded(child: Text(message, style: TextStyle(color: Colors.red[700], fontSize: AppFontSizes.bodySmall)))]));
  }
}
