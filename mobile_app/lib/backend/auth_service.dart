import 'package:firebase_auth/firebase_auth.dart';
import 'package:uuid/uuid.dart';
import 'package:flutter_timezone/flutter_timezone.dart';
import 'database_service.dart' show DatabaseService, UserProfile;

class AuthService {
  final FirebaseAuth _auth = FirebaseAuth.instance;
  final DatabaseService _dbService = DatabaseService();
  final Uuid _uuid = const Uuid();

  // Get current user
  User? get currentUser => _auth.currentUser;

  // Auth state changes stream
  Stream<User?> get authStateChanges => _auth.authStateChanges();

  // Initialize database schema (call this once at app startup)
  Future<void> initializeDatabase() async {
    await _dbService.initializeSchema();
  }

  // Sign up with username, password, first_name, last_name
  // Follows system design: creates Firebase user with username@wanda.com, then creates Postgres profile
  Future<UserProfile> signUp({required String username, required String password, required String firstName, required String lastName}) async {
    try {
      // Step 1: Create Firebase user with email format: username@wanda.com
      final email = '$username@wanda.com';
      final userCredential = await _auth.createUserWithEmailAndPassword(email: email, password: password);

      // Step 2: Get firebase_uid
      final firebaseUid = userCredential.user!.uid;

      // Step 3: Generate UUID v4 for user_id
      final appUuid = _uuid.v4();

      // Step 4: Idempotency check - if user already exists, return existing profile
      final existing = await _dbService.getUserByFirebaseUid(firebaseUid);
      if (existing != null) {
        return existing;
      }

      // Step 5: Get user's timezone (IANA format like "America/Los_Angeles")
      final timezone = await FlutterTimezone.getLocalTimezone();

      // Step 6: Insert profile into Postgres
      final profile = await _dbService.createUserProfile(userId: appUuid, firstName: firstName, lastName: lastName, firebaseUid: firebaseUid, username: username, timezone: timezone);

      return profile;
    } on FirebaseAuthException catch (e) {
      throw _handleAuthException(e);
    } catch (e) {
      // If Firebase user was created but Postgres insert failed, we could clean up
      // For prototype, just throw the error
      throw e.toString();
    }
  }

  // Sign in with username and password
  // Follows system design: signs in via Firebase, then ensures Postgres profile exists
  Future<UserProfile> signIn({required String username, required String password}) async {
    try {
      // Step 1: Sign in via Firebase Auth with email format: username@wanda.com
      final email = '$username@wanda.com';
      final userCredential = await _auth.signInWithEmailAndPassword(email: email, password: password);

      // Step 2: Get firebase_uid
      final firebaseUid = userCredential.user!.uid;

      // Step 3: Query Postgres for user profile
      var profile = await _dbService.getUserByFirebaseUid(firebaseUid);

      // Step 4: If profile missing (signup partially failed), we need to handle this
      // For now, throw an error - in production you might want to prompt for additional info
      if (profile == null) {
        throw 'User profile not found. Please contact support.';
      }

      return profile;
    } on FirebaseAuthException catch (e) {
      throw _handleAuthException(e);
    } catch (e) {
      throw e.toString();
    }
  }

  // Legacy method for backward compatibility (uses email directly)
  Future<UserCredential?> signInWithEmailAndPassword({required String email, required String password}) async {
    try {
      final userCredential = await _auth.signInWithEmailAndPassword(email: email, password: password);
      return userCredential;
    } on FirebaseAuthException catch (e) {
      throw _handleAuthException(e);
    } catch (e) {
      throw 'An unexpected error occurred: $e';
    }
  }

  // Legacy method for backward compatibility
  Future<UserCredential?> signUpWithEmailAndPassword({required String email, required String password}) async {
    try {
      final userCredential = await _auth.createUserWithEmailAndPassword(email: email, password: password);
      return userCredential;
    } on FirebaseAuthException catch (e) {
      throw _handleAuthException(e);
    } catch (e) {
      throw 'An unexpected error occurred: $e';
    }
  }

  // Sign out
  Future<void> signOut() async {
    try {
      await _auth.signOut();
    } catch (e) {
      throw 'Error signing out: $e';
    }
  }

  // Send password reset email
  Future<void> sendPasswordResetEmail(String email) async {
    try {
      await _auth.sendPasswordResetEmail(email: email);
    } on FirebaseAuthException catch (e) {
      throw _handleAuthException(e);
    } catch (e) {
      throw 'An unexpected error occurred: $e';
    }
  }

  // Handle Firebase auth exceptions
  String _handleAuthException(FirebaseAuthException e) {
    switch (e.code) {
      case 'weak-password':
        return 'The password provided is too weak.';
      case 'email-already-in-use':
        return 'An account already exists for that email.';
      case 'user-not-found':
        return 'No user found for that email.';
      case 'wrong-password':
        return 'Wrong password provided.';
      case 'invalid-email':
        return 'The email address is invalid.';
      case 'user-disabled':
        return 'This user account has been disabled.';
      case 'too-many-requests':
        return 'Too many requests. Please try again later.';
      case 'operation-not-allowed':
        return 'This operation is not allowed.';
      default:
        return 'An error occurred: ${e.message}';
    }
  }
}
