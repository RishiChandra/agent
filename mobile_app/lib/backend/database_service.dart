import 'package:postgres/postgres.dart' show Connection, Endpoint, UniqueViolationException, PgException, Sql;

class UserProfile {
  final String userId;
  final String firstName;
  final String lastName;
  final String firebaseUid;
  final String username;

  UserProfile({required this.userId, required this.firstName, required this.lastName, required this.firebaseUid, required this.username});

  Map<String, dynamic> toMap() {
    return {'user_id': userId, 'first_name': firstName, 'last_name': lastName, 'firebase_uid': firebaseUid, 'username': username};
  }

  factory UserProfile.fromMap(Map<String, dynamic> map) {
    return UserProfile(userId: map['user_id'] as String, firstName: map['first_name'] as String, lastName: map['last_name'] as String, firebaseUid: map['firebase_uid'] as String, username: map['username'] as String);
  }
}

class DatabaseService {
  // WARNING: This is prototype-only. Credentials are exposed in the app.
  // For production, use a backend API instead.
  //
  // Database credentials from .env file
  // The database must be publicly accessible over the internet with SSL enabled.
  // Ensure the users table has UNIQUE constraints on both username and firebase_uid.
  static const String _host = 'ai-pin-server.postgres.database.azure.com';
  static const int _port = 5432;
  static const String _database = 'postgres';
  static const String _username = 'sssdddaaaa';
  static const String _password = 'Jymeisit1234';

  Connection? _connection;

  Future<Connection> _getConnection() async {
    if (_connection != null) {
      return _connection!;
    }

    _connection = await Connection.open(Endpoint(host: _host, port: _port, database: _database, username: _username, password: _password));

    return _connection!;
  }

  Future<void> close() async {
    await _connection?.close();
  }

  // Initialize database schema (create users table if it doesn't exist)
  Future<void> initializeSchema() async {
    final conn = await _getConnection();
    await conn.execute('''
      CREATE TABLE IF NOT EXISTS users (
        user_id UUID PRIMARY KEY,
        first_name TEXT NOT NULL,
        last_name TEXT NOT NULL,
        firebase_uid TEXT NOT NULL UNIQUE,
        username TEXT NOT NULL UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
      )
    ''');
  }

  // Idempotency check: Get user by firebase_uid
  Future<UserProfile?> getUserByFirebaseUid(String firebaseUid) async {
    try {
      final conn = await _getConnection();
      final result = await conn.execute(Sql.named('SELECT user_id, first_name, last_name, firebase_uid, username FROM users WHERE firebase_uid = @firebaseUid'), parameters: {'firebaseUid': firebaseUid});

      if (result.isEmpty) {
        return null;
      }

      final row = result.first;
      return UserProfile(userId: row[0] as String, firstName: row[1] as String, lastName: row[2] as String, firebaseUid: row[3] as String, username: row[4] as String);
    } catch (e) {
      throw 'Database error: $e';
    }
  }

  // Create user profile with idempotency check
  Future<UserProfile> createUserProfile({required String userId, required String firstName, required String lastName, required String firebaseUid, required String username}) async {
    try {
      final conn = await _getConnection();

      // Idempotency check: if user already exists, return existing profile
      final existing = await getUserByFirebaseUid(firebaseUid);
      if (existing != null) {
        return existing;
      }

      // Insert new user
      await conn.execute(
        Sql.named('''
        INSERT INTO users (user_id, first_name, last_name, firebase_uid, username)
        VALUES (@userId, @firstName, @lastName, @firebaseUid, @username)
      '''),
        parameters: {'userId': userId, 'firstName': firstName, 'lastName': lastName, 'firebaseUid': firebaseUid, 'username': username},
      );

      return UserProfile(userId: userId, firstName: firstName, lastName: lastName, firebaseUid: firebaseUid, username: username);
    } on UniqueViolationException catch (e) {
      // Handle unique constraint violations
      if (e.message.contains('username') || e.constraintName?.contains('username') == true) {
        throw 'Username is already taken';
      }
      if (e.message.contains('firebase_uid') || e.constraintName?.contains('firebase_uid') == true) {
        throw 'User already exists';
      }
      throw 'Username is already taken';
    } on PgException catch (e) {
      throw 'Database error: ${e.message}';
    } catch (e) {
      throw 'Database error: $e';
    }
  }
}
