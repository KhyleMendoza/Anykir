import 'package:anykir_mobile/protocol.dart';

Future<void> main() async {
  for (final (ip, want) in [('100.123.9.43', true), ('100.64.0.1', true), ('100.128.0.1', false), ('192.168.210.112', false)]) {
    if (isTailscaleIp(ip) != want) throw 'isTailscaleIp($ip) should be $want';
  }
  print('this PC Tailscale: ${await phoneTailscaleIp()}');
  print('split: ${splitServers('192.168.210.112:7000, 100.123.9.43:7000')}');
  final (live, pcs) = await fetchOnlineAny(['127.0.0.1:1', '100.123.9.43:7000']);
  print('dead server skipped, answered: $live, PCs: ${pcs.length}');
  for (final pc in pcs) {
    print('  ${pc.name}  ${pc.address}  tailscale=${pc.tailscaleAddress}');
  }
  print('ALL OK');
}
