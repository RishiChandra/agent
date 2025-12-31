import 'package:flutter/material.dart';
import 'package:firebase_core/firebase_core.dart';
import 'backend/auth_service.dart';
import 'screens/splash_screen.dart';

void main() async {
  WidgetsFlutterBinding.ensureInitialized();
  await Firebase.initializeApp();

  // Initialize database schema
  final authService = AuthService();
  try {
    await authService.initializeDatabase();
  } catch (e) {
    // Continue anyway - user will see error when trying to sign up/sign in
    // Database initialization errors will be handled when user tries to authenticate
  }

  runApp(const MyApp());
}

class MyApp extends StatelessWidget {
  const MyApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(debugShowCheckedModeBanner: false, theme: ThemeData(colorScheme: ColorScheme.fromSeed(seedColor: Colors.deepPurple), useMaterial3: true), home: const SplashScreen());
  }
}
