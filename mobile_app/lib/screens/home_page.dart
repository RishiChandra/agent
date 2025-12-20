import 'package:flutter/material.dart';
import '../utils/design_system.dart';
import '../utils/widgets/app_button.dart';
import '../utils/widgets/app_page_header.dart';
import 'sign_in_page.dart';
import 'sign_up_page.dart';

class HomePage extends StatelessWidget {
  const HomePage({super.key});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      body: SafeArea(
        child: Center(
          child: Padding(
            padding: const EdgeInsets.all(AppSpacing.lg),
            child: Column(
              mainAxisAlignment: MainAxisAlignment.center,
              children: [
                AppPageHeader(icon: Icons.health_and_safety, title: 'AI Health Assistant', subtitle: 'Your personal health companion', iconSize: AppSizes.iconXLarge),
                const SizedBox(height: AppSpacing.xxxl),
                AppPrimaryButton(
                  text: 'Sign In',
                  onPressed: () {
                    Navigator.push(context, MaterialPageRoute(builder: (_) => const SignInPage()));
                  },
                ),
                const SizedBox(height: AppSpacing.md),
                AppSecondaryButton(
                  text: 'Sign Up',
                  onPressed: () {
                    Navigator.push(context, MaterialPageRoute(builder: (_) => const SignUpPage()));
                  },
                ),
              ],
            ),
          ),
        ),
      ),
    );
  }
}
