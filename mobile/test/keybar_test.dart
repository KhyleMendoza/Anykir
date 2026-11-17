import 'dart:io';

import 'package:anykir_mobile/main.dart';
import 'package:anykir_mobile/session.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  final host = Platform.environment['ANYKIR_HOST'], pw = Platform.environment['ANYKIR_PW'];
  testWidgets('key bar and Enter', (tester) async {
    SharedPreferences.setMockInitialValues({});
    tester.view.physicalSize = const Size(1080, 2340);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    final s = (await tester.runAsync(() => ViewerSession.open(address: host!, password: pw!)))!;
    final keys = <String>[];
    s.debugKeySent = keys.add;
    await tester.pumpWidget(MaterialApp(home: SessionPage(session: s, address: host!, password: pw!, servers: const [])));
    await tester.pump(const Duration(seconds: 1));
    await tester.pump(const Duration(seconds: 6));
    await tester.pump(const Duration(seconds: 1));

    await tester.tap(find.byIcon(Icons.apps_rounded));
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    await tester.tap(find.byIcon(Icons.keyboard_alt_outlined));
    await tester.pump(const Duration(milliseconds: 400));

    expect(find.byTooltip('Paste'), findsOneWidget);
    expect(find.byTooltip('Show keyboard'), findsOneWidget);
    expect(find.byTooltip('Hide keyboard'), findsOneWidget);
    expect(find.text('Ctrl'), findsNothing);
    expect(find.text('F5'), findsNothing);

    ScaffoldMessenger.of(tester.element(find.byType(Scaffold).first)).clearSnackBars();
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 100));
    }
    final before = tester.getCenter(find.byTooltip('Paste'));
    final finger = await tester.startGesture(tester.getCenter(find.byTooltip('Show keyboard')));
    for (var i = 0; i < 8; i++) {
      await finger.moveBy(const Offset(-15, -10));
      await tester.pump(const Duration(milliseconds: 16));
    }
    await finger.up();
    await tester.pump();
    final after = tester.getCenter(find.byTooltip('Paste'));
    expect(after.dx, lessThan(before.dx - 100));
    expect(after.dy, lessThan(before.dy - 60));

    await tester.longPress(find.byTooltip('Hide keyboard'));
    await tester.pump();
    expect(find.byTooltip('Paste'), findsNothing);
    expect(find.byTooltip('Keyboard - hold for all buttons'), findsOneWidget);
    await tester.longPress(find.byTooltip('Keyboard - hold for all buttons'));
    await tester.pump();
    expect(find.byTooltip('Paste'), findsOneWidget);
    expect(find.byTooltip('Keyboard - hold for all buttons'), findsNothing);

    final finger2 = await tester.startGesture(tester.getCenter(find.byTooltip('Show keyboard')));
    for (var i = 0; i < 20; i++) {
      await finger2.moveBy(const Offset(0, -60));
      await tester.pump(const Duration(milliseconds: 16));
    }
    await finger2.up();
    await tester.pump();
    expect(tester.getRect(find.byTooltip('Paste')).top, greaterThanOrEqualTo(0));

    await tester.testTextInput.receiveAction(TextInputAction.newline);
    await tester.pump();
    expect(keys, ['Return']);
    expect(tester.testTextInput.isVisible, isTrue, reason: 'keyboard stays open after Enter');
    await tester.testTextInput.receiveAction(TextInputAction.newline);
    await tester.pump();
    expect(keys, ['Return', 'Return']);

    tester.testTextInput.enterText('  hi');
    await tester.pump();
    expect(keys, ['Return', 'Return', 'h', 'i']);

    ScaffoldMessenger.of(tester.element(find.byType(Scaffold).first)).clearSnackBars();
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 100));
    }
    await tester.tap(find.byIcon(Icons.keyboard_command_key));
    await tester.pump();
    expect(find.text('Ctrl'), findsOneWidget);
    await tester.scrollUntilVisible(find.text('Esc'), 80, scrollable: find.byType(Scrollable).last);
    await tester.ensureVisible(find.text('Esc'));
    await tester.pump(const Duration(milliseconds: 300));
    expect(find.text('Esc'), findsOneWidget);
    await tester.tap(find.text('Esc'));
    await tester.pump();
    expect(keys.last, 'Escape');
    await tester.scrollUntilVisible(find.byTooltip('Fewer keys'), -80, scrollable: find.byType(Scrollable).last);
    await tester.pump(const Duration(milliseconds: 300));
    await tester.tap(find.byTooltip('Fewer keys'));
    await tester.pump();
    expect(find.text('Ctrl'), findsNothing);

    await tester.tap(find.byTooltip('Hide keyboard'));
    await tester.pump();
    expect(find.byTooltip('Paste'), findsNothing);

    s.close();
    await tester.pumpWidget(const SizedBox());
    await tester.pump(const Duration(seconds: 2));
  }, skip: host == null || pw == null);

  testWidgets('view only (chosen before connecting) sends nothing', (tester) async {
    SharedPreferences.setMockInitialValues({});
    tester.view.physicalSize = const Size(1080, 2340);
    tester.view.devicePixelRatio = 3;
    addTearDown(tester.view.reset);
    final s = (await tester.runAsync(() => ViewerSession.open(address: host!, password: pw!)))!;
    final sent = <String>[];
    s.debugInput = sent.add;
    await tester.pumpWidget(
        MaterialApp(home: SessionPage(session: s, address: host!, password: pw!, servers: const [], viewOnly: true)));
    await tester.pump(const Duration(seconds: 1));
    await tester.pump(const Duration(seconds: 6));
    await tester.pump(const Duration(seconds: 1));
    expect(s.viewOnly, isTrue);
    expect(find.text('View only'), findsOneWidget);

    const c = Offset(80, 300);
    await tester.tapAt(c);
    await tester.pump(const Duration(milliseconds: 400));
    await tester.dragFrom(c, const Offset(120, 60));
    await tester.pump(const Duration(milliseconds: 600));
    final a = await tester.startGesture(c - const Offset(40, 0), pointer: 7);
    final b = await tester.startGesture(c + const Offset(40, 0), pointer: 8);
    await a.moveBy(const Offset(-80, 0));
    await b.moveBy(const Offset(80, 0));
    await a.up();
    await b.up();
    await tester.pump(const Duration(milliseconds: 600));
    expect(sent, isEmpty, reason: 'view only must not send input');

    await tester.tap(find.byIcon(Icons.apps_rounded));
    for (var i = 0; i < 10; i++) {
      await tester.pump(const Duration(milliseconds: 50));
    }
    for (final label in ['Keyboard', 'Mouse', 'Shortcuts', 'Ctrl+Alt+Del', 'Take control']) {
      expect(find.text(label), findsNothing, reason: label);
    }
    expect(find.text('Disconnect'), findsOneWidget);
    await tester.tap(find.text('View only'), warnIfMissed: false);
    await tester.pump(const Duration(milliseconds: 600));
    expect(s.viewOnly, isTrue);
    expect(sent, isEmpty);

    s.close();
    await tester.pumpWidget(const SizedBox());
    await tester.pump(const Duration(seconds: 2));
  }, skip: host == null || pw == null);
}
