import 'package:flutter/material.dart';
import 'package:flutter_esp_ble_prov/flutter_esp_ble_prov.dart';
import 'package:shared_preferences/shared_preferences.dart';
import '../backend/auth_service.dart';
import 'home_page.dart';

class EspProvPage extends StatefulWidget {
  const EspProvPage({super.key});
  @override
  State<EspProvPage> createState() => _EspProvPageState();
}

class _EspProvPageState extends State<EspProvPage> {
  final prov = FlutterEspBleProv();
  final PageController _pageController = PageController();
  final AuthService _authService = AuthService();

  final prefixCtrl = TextEditingController(text: 'PROV_');
  final passCtrl = TextEditingController();

  List<String> devices = [];
  List<String> networks = [];
  String deviceName = '';
  String selectedSsid = '';
  String status = '';
  int _currentStep = 0;

  Future<void> _signOut() async {
    try {
      await _authService.signOut();
      // Clear local storage
      final prefs = await SharedPreferences.getInstance();
      await prefs.setBool('is_signed_in', false);
      await prefs.remove('user_id');

      // Navigate to home page
      if (mounted) {
        Navigator.of(context).pushAndRemoveUntil(MaterialPageRoute(builder: (_) => const HomePage()), (route) => false);
      }
    } catch (e) {
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text('Error signing out: $e')));
      }
    }
  }

  Future<void> scanBle() async {
    try {
      devices = await prov.scanBleDevices(prefixCtrl.text);
    } catch (e) {
      setState(() => status = 'Error scanning BLE: $e');
      return;
    }
    setState(() {});
  }

  Future<void> scanWifi() async {
    try {
      networks = await prov.scanWifiNetworks(deviceName, 'abcd1234'); // Default proof of possession
      selectedSsid = ''; // Clear selection when scanning new networks
      setState(() => status = networks.isEmpty ? 'No WiFi networks found' : 'Found ${networks.length} networks');
    } catch (e) {
      setState(() => status = 'Error scanning WiFi: $e');
    }
  }

  void _nextStep() {
    if (_currentStep == 0 && deviceName.isNotEmpty) {
      _pageController.nextPage(duration: const Duration(milliseconds: 300), curve: Curves.easeInOut);
      setState(() => _currentStep = 1);
      scanWifi(); // Auto-scan WiFi when moving to step 1
    }
  }

  void _previousStep() {
    if (_currentStep > 0) {
      _pageController.previousPage(duration: const Duration(milliseconds: 300), curve: Curves.easeInOut);
      setState(() => _currentStep = 0);
    }
  }

  Future<void> doProvision() async {
    if (selectedSsid.isEmpty) {
      setState(() => status = 'Please select a WiFi network');
      return;
    }
    setState(() => status = 'Provisioning...');
    final ok = await prov.provisionWifi(deviceName, 'abcd1234', selectedSsid, passCtrl.text);
    setState(() => status = ok == true ? 'Provisioned OK' : 'Provision failed');
  }

  @override
  void dispose() {
    _pageController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      backgroundColor: Colors.white,
      appBar: AppBar(title: const Text('ESP BLE Provision'), scrolledUnderElevation: 0, surfaceTintColor: Colors.transparent, actions: [IconButton(icon: const Icon(Icons.logout), onPressed: _signOut, tooltip: 'Sign Out')]),
      body: Column(
        children: [
          Expanded(
            child: PageView(
              controller: _pageController,
              physics: const NeverScrollableScrollPhysics(), // Disable swipe
              children: [
                // Step 0: BLE Device Selection
                _buildBleStep(),
                // Step 1: WiFi Selection and Password
                _buildWifiStep(),
              ],
            ),
          ),
          // Navigation buttons
          Container(
            padding: const EdgeInsets.all(16),
            child: Row(
              mainAxisAlignment: MainAxisAlignment.spaceBetween,
              children: [
                // Back button (bottom left)
                if (_currentStep > 0) TextButton.icon(onPressed: _previousStep, icon: const Icon(Icons.arrow_back), label: const Text('Back')) else const SizedBox.shrink(),
                // Next/Provision button (bottom right)
                ElevatedButton.icon(onPressed: _currentStep == 0 ? (deviceName.isNotEmpty ? _nextStep : null) : (selectedSsid.isNotEmpty && passCtrl.text.isNotEmpty ? doProvision : null), icon: Icon(_currentStep == 0 ? Icons.arrow_forward : Icons.check), label: Text(_currentStep == 0 ? 'Next' : 'Provision'), style: ElevatedButton.styleFrom(backgroundColor: (_currentStep == 0 && deviceName.isEmpty) || (_currentStep == 1 && (selectedSsid.isEmpty || passCtrl.text.isEmpty)) ? Colors.grey[400] : null)),
              ],
            ),
          ),
        ],
      ),
    );
  }

  Widget _buildBleStep() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text('Step 1: Select BLE Device', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
          const SizedBox(height: 16),
          TextField(controller: prefixCtrl, decoration: const InputDecoration(labelText: 'Device prefix (e.g. PROV_)')),
          const SizedBox(height: 8),
          ElevatedButton(onPressed: scanBle, child: const Text('Scan BLE')),
          if (devices.isNotEmpty) ...[
            const SizedBox(height: 16),
            const Text('BLE Devices:', style: TextStyle(fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            SizedBox(
              height: 300,
              child: ListView(
                children:
                    devices
                        .map(
                          (d) => ListTile(
                            title: Text(d),
                            trailing: deviceName == d ? const Icon(Icons.check, color: Colors.blue) : null,
                            onTap: () {
                              deviceName = d;
                              networks = []; // Clear previous networks
                              selectedSsid = ''; // Clear previous selection
                              setState(() {});
                            },
                          ),
                        )
                        .toList(),
              ),
            ),
          ],
          if (status.isNotEmpty && _currentStep == 0) ...[const SizedBox(height: 16), Text(status, style: const TextStyle(fontWeight: FontWeight.bold))],
        ],
      ),
    );
  }

  Widget _buildWifiStep() {
    return SingleChildScrollView(
      padding: const EdgeInsets.all(16),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.stretch,
        children: [
          const Text('Step 2: Select WiFi Network', style: TextStyle(fontSize: 20, fontWeight: FontWeight.bold)),
          const SizedBox(height: 16),
          ElevatedButton(onPressed: scanWifi, child: const Text('Scan WiFi Networks')),
          if (networks.isNotEmpty) ...[
            const SizedBox(height: 16),
            const Text('Wi-Fi Networks:', style: TextStyle(fontWeight: FontWeight.bold)),
            const SizedBox(height: 8),
            SizedBox(
              height: 250,
              child: ListView(
                children:
                    networks
                        .map(
                          (n) => ListTile(
                            title: Text(n),
                            trailing: selectedSsid == n ? const Icon(Icons.check, color: Colors.green) : null,
                            onTap: () {
                              selectedSsid = n;
                              setState(() {});
                            },
                          ),
                        )
                        .toList(),
              ),
            ),
          ],
          if (selectedSsid.isNotEmpty) ...[const SizedBox(height: 16), Text('Selected Network: $selectedSsid', style: const TextStyle(fontWeight: FontWeight.bold))],
          const SizedBox(height: 16),
          TextField(
            controller: passCtrl,
            decoration: const InputDecoration(labelText: 'Wi-Fi password'),
            obscureText: true,
            onChanged: (_) => setState(() {}), // Update button state
          ),
          if (status.isNotEmpty && _currentStep == 1) ...[const SizedBox(height: 16), Text(status, style: const TextStyle(fontWeight: FontWeight.bold))],
        ],
      ),
    );
  }
}

