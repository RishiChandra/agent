import 'package:flutter/material.dart';

/// Design System - Standardized spacing, sizes, and styles
class AppSpacing {
  // Spacing values (in logical pixels)
  static const double xs = 4.0;
  static const double sm = 8.0;
  static const double md = 16.0;
  static const double lg = 24.0;
  static const double xl = 32.0;
  static const double xxl = 48.0;
  static const double xxxl = 64.0;

  // Common spacing combinations
  static const double paddingHorizontal = md;
  static const double paddingVertical = md;
  static const double screenPadding = md;
}

class AppSizes {
  // Button heights
  static const double buttonHeight = 50.0;
  static const double buttonHeightSmall = 40.0;
  static const double buttonHeightLarge = 56.0;

  // Icon sizes
  static const double iconSmall = 20.0;
  static const double iconMedium = 24.0;
  static const double iconLarge = 80.0;
  static const double iconXLarge = 120.0;

  // Border radius
  static const double radiusSmall = 8.0;
  static const double radiusMedium = 12.0;
  static const double radiusLarge = 16.0;

  // Input field heights
  static const double inputHeight = 50.0;
}

class AppFontSizes {
  // Font sizes
  static const double xs = 12.0;
  static const double sm = 14.0;
  static const double md = 16.0;
  static const double lg = 20.0;
  static const double xl = 28.0;
  static const double xxl = 32.0;

  // Specific use cases
  static const double body = md;
  static const double bodySmall = sm;
  static const double heading = xxl;
  static const double headingLarge = xxl;
  static const double subheading = lg;
  static const double caption = xs;
}

class AppTextStyles {
  // Heading styles
  static TextStyle heading(BuildContext context) {
    return TextStyle(fontSize: AppFontSizes.heading, fontWeight: FontWeight.bold);
  }

  static TextStyle headingLarge(BuildContext context) {
    return TextStyle(fontSize: AppFontSizes.headingLarge, fontWeight: FontWeight.bold);
  }

  static TextStyle subheading(BuildContext context) {
    return TextStyle(fontSize: AppFontSizes.subheading, fontWeight: FontWeight.w600);
  }

  // Body styles
  static TextStyle body(BuildContext context) {
    return TextStyle(fontSize: AppFontSizes.body);
  }

  static TextStyle bodySmall(BuildContext context) {
    return TextStyle(fontSize: AppFontSizes.bodySmall, color: Colors.grey[600]);
  }

  static TextStyle caption(BuildContext context) {
    return TextStyle(fontSize: AppFontSizes.caption, color: Colors.grey[600]);
  }

  // Button text styles
  static TextStyle buttonText(BuildContext context) {
    return TextStyle(fontSize: AppFontSizes.md, fontWeight: FontWeight.bold);
  }
}
