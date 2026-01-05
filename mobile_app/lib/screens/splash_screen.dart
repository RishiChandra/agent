import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../utils/design_system.dart';
import '../utils/widgets/app_page_header.dart';
import 'home_page.dart';
import 'tasks_page.dart';
import '../backend/auth_service.dart';
import '../backend/database_service.dart';

class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen> {
  final AuthService _authService = AuthService();
  final DatabaseService _dbService = DatabaseService();

  @override
  void initState() {
    super.initState();
    _checkAuthAndNavigate();
  }

  Future<void> _checkAuthAndNavigate() async {
    // Wait for 2 seconds
    await Future.delayed(const Duration(seconds: 2));

    if (!mounted) return;

    // Check local storage for auth state
    final prefs = await SharedPreferences.getInstance();
    final isSignedIn = prefs.getBool('is_signed_in') ?? false;

    if (isSignedIn) {
      // Get current Firebase user
      final currentUser = _authService.currentUser;
      if (currentUser != null) {
        try {
          // Get user profile from database
          final profile = await _dbService.getUserByFirebaseUid(currentUser.uid);
          if (profile != null && mounted) {
            // Navigate to tasks page
            Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => TasksPage(userId: profile.userId)));
            return;
          }
        } catch (e) {
          // If we can't get the profile, fall through to home page
        }
      }
      // If we can't get the user or profile, navigate to home page
      if (mounted) {
        Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const HomePage()));
      }
    } else {
      // Navigate to home page
      if (mounted) {
        Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const HomePage()));
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(backgroundColor: Colors.white, body: Center(child: Column(mainAxisAlignment: MainAxisAlignment.center, children: [AppPageHeader(icon: Icons.health_and_safety, title: 'AI Health Assistant', iconSize: 100), const SizedBox(height: AppSpacing.xxl), const CircularProgressIndicator()])));
  }
}
