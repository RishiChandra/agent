import 'package:flutter/material.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../utils/design_system.dart';
import '../utils/widgets/app_page_header.dart';
import 'home_page.dart';
import 'esp_prov_page.dart';

class SplashScreen extends StatefulWidget {
  const SplashScreen({super.key});

  @override
  State<SplashScreen> createState() => _SplashScreenState();
}

class _SplashScreenState extends State<SplashScreen> {
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
      // Navigate to bluetooth page
      if (mounted) {
        Navigator.of(context).pushReplacement(MaterialPageRoute(builder: (_) => const EspProvPage()));
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
