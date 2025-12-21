import 'package:flutter/material.dart';
import '../design_system.dart';

/// Standardized page header with icon, title, and subtitle
class AppPageHeader extends StatelessWidget {
  final IconData icon;
  final String title;
  final String? subtitle;
  final double iconSize;

  const AppPageHeader({super.key, required this.icon, required this.title, this.subtitle, this.iconSize = AppSizes.iconLarge});

  @override
  Widget build(BuildContext context) {
    return Column(
      children: [
        Icon(icon, size: iconSize, color: Theme.of(context).colorScheme.primary),
        const SizedBox(height: AppSpacing.md),
        Text(title, style: AppTextStyles.heading(context), textAlign: TextAlign.center),
        if (subtitle != null) ...[const SizedBox(height: AppSpacing.sm), Text(subtitle!, style: AppTextStyles.bodySmall(context), textAlign: TextAlign.center)],
      ],
    );
  }
}
