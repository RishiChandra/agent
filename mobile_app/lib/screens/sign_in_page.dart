import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../backend/auth_service.dart';
import 'esp_prov_page.dart';
import '../utils/design_system.dart';
import '../utils/widgets/app_button.dart';
import '../utils/widgets/app_text_input.dart';
import '../utils/widgets/app_error_message.dart';
import '../utils/widgets/app_page_header.dart';

class SignInPage extends StatefulWidget {
  const SignInPage({super.key});

  @override
  State<SignInPage> createState() => _SignInPageState();
}

class _SignInPageState extends State<SignInPage> {
  final AuthService _authService = AuthService();
  final _formKey = GlobalKey<FormState>();
  final _usernameController = TextEditingController();
  final _passwordController = TextEditingController();

  bool _isLoading = false;
  String? _errorMessage;

  @override
  void dispose() {
    _usernameController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  Future<void> _handleSignIn() async {
    if (!_formKey.currentState!.validate()) {
      return;
    }

    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    try {
      await _authService.signIn(username: _usernameController.text.trim(), password: _passwordController.text);

      // Save to local storage
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('is_signed_in', true);

      // Navigate to bluetooth page
      if (mounted) {
        Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const EspProvPage()), (route) => false);
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

  Future<void> _handlePasswordReset() async {
    if (_usernameController.text.trim().isEmpty) {
      setState(() {
        _errorMessage = 'Please enter your username first.';
      });
      return;
    }

    setState(() {
      _isLoading = true;
      _errorMessage = null;
    });

    try {
      final email = '${_usernameController.text.trim()}@wanda.com';
      await _authService.sendPasswordResetEmail(email);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(const SnackBar(content: Text('Password reset email sent! Check your inbox.'), backgroundColor: Colors.green));
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
      appBar: AppBar(title: const Text('Sign In'), scrolledUnderElevation: 0, surfaceTintColor: Colors.transparent),
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
                  AppPageHeader(icon: Icons.lock_outline, title: 'Welcome Back', subtitle: 'Sign in to continue'),
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

                  // Password field
                  AppPasswordInput(
                    controller: _passwordController,
                    labelText: 'Password',
                    textInputAction: TextInputAction.done,
                    validator: (value) {
                      if (value == null || value.isEmpty) {
                        return 'Please enter your password';
                      }
                      return null;
                    },
                  ),
                  const SizedBox(height: AppSpacing.md),

                  // Error message
                  if (_errorMessage != null) AppErrorMessage(message: _errorMessage!),

                  // Sign in button
                  AppPrimaryButton(text: 'Sign In', onPressed: _handleSignIn, isLoading: _isLoading),
                  const SizedBox(height: AppSpacing.md),

                  // Forgot password
                  AppTextButton(text: 'Forgot Password?', onPressed: _isLoading ? null : _handlePasswordReset),
                ],
              ),
            ),
          ),
        ),
      ),
    );
  }
}
