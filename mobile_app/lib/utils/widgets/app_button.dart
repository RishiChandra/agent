import 'package:flutter/material.dart';
import '../design_system.dart';

/// Primary button - filled with primary color
class AppPrimaryButton extends StatelessWidget {
  final String text;
  final VoidCallback? onPressed;
  final bool isLoading;
  final double? width;
  final double? height;

  const AppPrimaryButton({super.key, required this.text, this.onPressed, this.isLoading = false, this.width, this.height});

  @override
  Widget build(BuildContext context) {
    return SizedBox(width: width ?? double.infinity, height: height ?? AppSizes.buttonHeight, child: ElevatedButton(onPressed: isLoading ? null : onPressed, style: ElevatedButton.styleFrom(shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppSizes.radiusMedium)), backgroundColor: Theme.of(context).colorScheme.primary, foregroundColor: Colors.white, disabledBackgroundColor: Colors.grey[400]), child: isLoading ? const SizedBox(height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2, valueColor: AlwaysStoppedAnimation<Color>(Colors.white))) : Text(text, style: AppTextStyles.buttonText(context))));
  }
}

/// Secondary button - outlined with primary color
class AppSecondaryButton extends StatelessWidget {
  final String text;
  final VoidCallback? onPressed;
  final bool isLoading;
  final double? width;
  final double? height;

  const AppSecondaryButton({super.key, required this.text, this.onPressed, this.isLoading = false, this.width, this.height});

  @override
  Widget build(BuildContext context) {
    return SizedBox(width: width ?? double.infinity, height: height ?? AppSizes.buttonHeight, child: OutlinedButton(onPressed: isLoading ? null : onPressed, style: OutlinedButton.styleFrom(shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppSizes.radiusMedium)), side: BorderSide(color: Theme.of(context).colorScheme.primary, width: 2), disabledForegroundColor: Colors.grey[400]), child: isLoading ? const SizedBox(height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2)) : Text(text, style: AppTextStyles.buttonText(context).copyWith(color: Theme.of(context).colorScheme.primary))));
  }
}

/// Text button - minimal style
class AppTextButton extends StatelessWidget {
  final String text;
  final VoidCallback? onPressed;
  final bool isLoading;

  const AppTextButton({super.key, required this.text, this.onPressed, this.isLoading = false});

  @override
  Widget build(BuildContext context) {
    return TextButton(onPressed: isLoading ? null : onPressed, child: isLoading ? const SizedBox(height: 20, width: 20, child: CircularProgressIndicator(strokeWidth: 2)) : Text(text));
  }
}

/// Icon button with text
class AppIconButton extends StatelessWidget {
  final String text;
  final IconData icon;
  final VoidCallback? onPressed;
  final bool isLoading;

  const AppIconButton({super.key, required this.text, required this.icon, this.onPressed, this.isLoading = false});

  @override
  Widget build(BuildContext context) {
    return SizedBox(height: AppSizes.buttonHeight, child: ElevatedButton.icon(onPressed: isLoading ? null : onPressed, icon: Icon(icon), label: Text(text), style: ElevatedButton.styleFrom(shape: RoundedRectangleBorder(borderRadius: BorderRadius.circular(AppSizes.radiusMedium)), backgroundColor: Theme.of(context).colorScheme.primary, foregroundColor: Colors.white)));
  }
}
