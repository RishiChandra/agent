import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../backend/auth_service.dart';
import 'tasks_page.dart';
import '../utils/design_system.dart';
import '../utils/widgets/app_button.dart';
import '../utils/widgets/app_text_input.dart';
import '../utils/widgets/app_error_message.dart';
import '../utils/widgets/app_page_header.dart';

class SignUpPage extends StatefulWidget {
  const SignUpPage({super.key});

  @override
  State<SignUpPage> createState() => _SignUpPageState();
}

class _SignUpPageState extends State<SignUpPage> {
  final AuthService _authService = AuthService();
  final _formKey = GlobalKey<FormState>();
  final _usernameController = TextEditingController();
  final _firstNameController = TextEditingController();
  final _lastNameController = TextEditingController();
  final _passwordController = TextEditingController();
  final _confirmPasswordController = TextEditingController();

  bool _isLoading = false;
  String? _errorMessage;

  @override
  void dispose() {
    _usernameController.dispose();
    _firstNameController.dispose();
    _lastNameController.dispose();
    _passwordController.dispose();
    _confirmPasswordController.dispose();
    super.dispose();
  }

  Future<void> _handleSignUp() async {
    if (!_formKey.currentState!.validate()) {
      return;
    }

    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    try {
      final profile = await _authService.signUp(username: _usernameController.text.trim(), password: _passwordController.text, firstName: _firstNameController.text.trim(), lastName: _lastNameController.text.trim());

      // Save to local storage
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('is_signed_in', true);
      await prefs.setString('user_id', profile.userId);

      // Navigate to tasks page
      if (mounted) {
        Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => TasksPage(userId: profile.userId)), (route) => false);
      }
    } catch (e) {
      setState(() {
        _errorMessage = e.toString();
      });
    } finally {
      if (mounted) {
        setState(() {
          _isLoading = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(title: const Text('Sign Up'), scrolledUnderElevation: 0, surfaceTintColor: Colors.transparent),
      body: SafeArea(
        child: Center(
          child: SingleChildScrollView(
            padding: const EdgeInsets.all(AppSpacing.lg),
            child: Form(
              key: _formKey,
              child: Column(
                mainAxisAlignment: MainAxisAlignment.center,
                crossAxisAlignment: CrossAxisAlignment.stretch,
                children: [
                  AppPageHeader(icon: Icons.person_add_outlined, title: 'Create Account', subtitle: 'Sign up to get started'),
                  const SizedBox(height: AppSpacing.xl),

                  // Username field
                  AppTextInput(
                    controller: _usernameController,
                    labelText: 'Username',
                    prefixIcon: Icons.person_outlined,
                    textInputAction: TextInputAction.next,
                    validator: (value) {
                      if (value == null || value.isEmpty) {
                        return 'Please enter your username';
                      }
                      if (value.length < 3) {
                        return 'Username must be at least 3 characters';
                      }
                      if (!RegExp(r'^[a-zA-Z0-9_]+$').hasMatch(value)) {
                        return 'Username can only contain letters, numbers, and underscores';
                      }
                      return null;
                    },
                  ),
                  const SizedBox(height: AppSpacing.md),

                  // First name field
                  AppTextInput(
                    controller: _firstNameController,
                    labelText: 'First Name',
                    prefixIcon: Icons.person_outlined,
                    textInputAction: TextInputAction.next,
                    validator: (value) {
                      if (value == null || value.isEmpty) {
                        return 'Please enter your first name';
                      }
                      return null;
                    },
                  ),
                  const SizedBox(height: AppSpacing.md),

                  // Last name field
                  AppTextInput(
                    controller: _lastNameController,
                    labelText: 'Last Name',
                    prefixIcon: Icons.person_outlined,
                    textInputAction: TextInputAction.next,
                    validator: (value) {
                      if (value == null || value.isEmpty) {
                        return 'Please enter your last name';
                      }
                      return null;
                    },
                  ),
                  const SizedBox(height: AppSpacing.md),

                  // Password field
                  AppPasswordInput(
                    controller: _passwordController,
                    labelText: 'Password',
                    textInputAction: TextInputAction.next,
                    validator: (value) {
                      if (value == null || value.isEmpty) {
                        return 'Please enter your password';
                      }
                      if (value.length < 6) {
                        return 'Password must be at least 6 characters';
                      }
                      return null;
                    },
                  ),
                  const SizedBox(height: AppSpacing.md),

                  // Confirm password field
                  AppPasswordInput(
                    controller: _confirmPasswordController,
                    labelText: 'Confirm Password',
                    textInputAction: TextInputAction.done,
                    validator: (value) {
                      if (value == null || value.isEmpty) {
                        return 'Please confirm your password';
                      }
                      if (value != _passwordController.text) {
                        return 'Passwords do not match';
                      }
                      return null;
                    },
                  ),
                  const SizedBox(height: AppSpacing.md),

                  // Error message
                  if (_errorMessage != null) AppErrorMessage(message: _errorMessage!),

                  // Sign up button
                  AppPrimaryButton(text: 'Sign Up', onPressed: _handleSignUp, isLoading: _isLoading),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
