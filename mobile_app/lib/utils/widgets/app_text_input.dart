import 'package:flutter/material.dart';
import '../design_system.dart';

/// Standardized text input field
class AppTextInput extends StatelessWidget {
  final TextEditingController controller;
  final String labelText;
  final IconData? prefixIcon;
  final bool obscureText;
  final TextInputType? keyboardType;
  final TextInputAction? textInputAction;
  final String? Function(String?)? validator;
  final VoidCallback? onTap;
  final bool readOnly;
  final Widget? suffixIcon;
  final void Function(String)? onChanged;

  const AppTextInput({super.key, required this.controller, required this.labelText, this.prefixIcon, this.obscureText = false, this.keyboardType, this.textInputAction, this.validator, this.onTap, this.readOnly = false, this.suffixIcon, this.onChanged});

  @override
  Widget build(BuildContext context) {
    return TextFormField(controller: controller, obscureText: obscureText, keyboardType: keyboardType, textInputAction: textInputAction, validator: validator, onTap: onTap, readOnly: readOnly, onChanged: onChanged, decoration: InputDecoration(labelText: labelText, prefixIcon: prefixIcon != null ? Icon(prefixIcon) : null, suffixIcon: suffixIcon, border: OutlineInputBorder(borderRadius: BorderRadius.circular(AppSizes.radiusMedium)), filled: true, fillColor: Colors.grey[50]));
  }
}

/// Password input field with visibility toggle
class AppPasswordInput extends StatefulWidget {
  final TextEditingController controller;
  final String labelText;
  final String? Function(String?)? validator;
  final TextInputAction? textInputAction;
  final void Function(String)? onChanged;

  const AppPasswordInput({super.key, required this.controller, required this.labelText, this.validator, this.textInputAction, this.onChanged});

  @override
  State<AppPasswordInput> createState() => _AppPasswordInputState();
}

class _AppPasswordInputState extends State<AppPasswordInput> {
  bool _obscureText = true;

  @override
  Widget build(BuildContext context) {
    return AppTextInput(
      controller: widget.controller,
      labelText: widget.labelText,
      prefixIcon: Icons.lock_outlined,
      obscureText: _obscureText,
      textInputAction: widget.textInputAction,
      validator: widget.validator,
      onChanged: widget.onChanged,
      suffixIcon: IconButton(
        icon: Icon(_obscureText ? Icons.visibility_outlined : Icons.visibility_off_outlined),
        onPressed: () {
          setState(() {
            _obscureText = !_obscureText;
          });
        },
      ),
    );
  }
}
