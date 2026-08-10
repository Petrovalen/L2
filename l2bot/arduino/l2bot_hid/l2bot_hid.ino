/*
  l2bot HID-мост для Arduino Leonardo / Micro / Pro Micro (чип ATmega32U4).

  Зачем: после апдейта игра перестала принимать синтетический ввод (SendInput).
  Arduino с нативным USB-HID шлёт нажатия как НАСТОЯЩАЯ клавиатура/мышь, поэтому
  игра их принимает. Питон-бот шлёт этому скетчу построчные команды по USB Serial
  (115200), а скетч выполняет их через Keyboard/Mouse.

  ВАЖНО (стабильность): приём команд сделан на ФИКСИРОВАННОМ char-буфере, без
  String. У ATmega32U4 всего ~2.5 КБ ОЗУ, а String на тысячах команд фрагментирует
  кучу — через ~час выделения начинают срываться и команды теряются/приходят
  битыми (симптом: «с первого раза клавиша не срабатывает»). Фиксированный буфер
  этого не допускает.

  Протокол (одна команда = одна строка, оканчивается '\n'):
    PING                 -> отвечает "PONG" (проверка, что прошита наша прошивка)
    KEY <name>           -> нажать+отпустить клавишу. <name>:
                              один печатный символ: 2 4 a b ...
                              f1..f12
                              enter esc space tab backspace delete
                              up down left right ctrl shift alt
    MOVE <dx> <dy> <ms>   -> плавно сдвинуть мышь на (dx,dy) за ms мс (относительно)
    CLICK                 -> левый клик
    RCLICK                -> правый клик
    LDOWN / LUP           -> зажать / отпустить левую кнопку
    RDOWN / RUP           -> зажать / отпустить правую кнопку
    DRAG <dx> <dy> <ms>   -> зажать ПКМ, плавно сдвинуть, отпустить (поворот камеры)

  При загрузке печатает "L2HID" — питон по этому опознаёт правильную прошивку.
*/
#include <Keyboard.h>
#include <Mouse.h>
#include <string.h>
#include <stdlib.h>

static char command[84];        // строка команды (без String — не фрагментирует кучу)
static uint8_t cmdLen = 0;

void setup() {
  Serial.begin(115200);
  Keyboard.begin();
  Mouse.begin();
  randomSeed(analogRead(A0));
  delay(300);
  Serial.println("L2HID");           // сигнал: наша прошивка загрузилась
}

// Человекоподобное относительное перемещение мыши (дуга + плавный разгон).
void smoothMove(int totalX, int totalY, int durationMs) {
  durationMs = constrain(durationMs, 40, 1500);
  int steps = constrain(durationMs / 12, 4, 90);
  int sentX = 0, sentY = 0;
  float distance = sqrt((float)totalX * totalX + (float)totalY * totalY);
  float curve = (float)random(-4, 5);
  for (int step = 1; step <= steps; step++) {
    float t = (float)step / (float)steps;
    float eased = t * t * (3.0f - 2.0f * t);           // плавный разгон/торможение
    float arc = sin(PI * t) * curve;
    float offsetX = distance > 0 ? (-totalY / distance) * arc : 0;
    float offsetY = distance > 0 ? ( totalX / distance) * arc : 0;
    int targetX = round(totalX * eased + offsetX);
    int targetY = round(totalY * eased + offsetY);
    int moveX = constrain(targetX - sentX, -127, 127);
    int moveY = constrain(targetY - sentY, -127, 127);
    Mouse.move(moveX, moveY, 0);
    sentX += moveX;
    sentY += moveY;
    delay(durationMs / steps + random(0, 4));
  }
}

// Перевести имя клавиши (C-строка) в HID-код. 0 = не распознано.
int keyCode(const char* n) {
  size_t len = strlen(n);
  if (len == 0) return 0;
  if (len == 1) return (uint8_t)n[0];                 // печатный символ: 2,4,a,...
  if ((n[0] == 'f' || n[0] == 'F') && len <= 3) {
    int num = atoi(n + 1);
    if (num >= 1 && num <= 12) return KEY_F1 + (num - 1);
  }
  if (!strcmp(n, "enter") || !strcmp(n, "return")) return KEY_RETURN;
  if (!strcmp(n, "esc")) return KEY_ESC;
  if (!strcmp(n, "space")) return ' ';
  if (!strcmp(n, "tab")) return KEY_TAB;
  if (!strcmp(n, "backspace")) return KEY_BACKSPACE;
  if (!strcmp(n, "delete")) return KEY_DELETE;
  if (!strcmp(n, "up")) return KEY_UP_ARROW;
  if (!strcmp(n, "down")) return KEY_DOWN_ARROW;
  if (!strcmp(n, "left")) return KEY_LEFT_ARROW;
  if (!strcmp(n, "right")) return KEY_RIGHT_ARROW;
  if (!strcmp(n, "ctrl")) return KEY_LEFT_CTRL;
  if (!strcmp(n, "shift")) return KEY_LEFT_SHIFT;
  if (!strcmp(n, "alt")) return KEY_LEFT_ALT;
  return 0;
}

void pressKey(const char* name) {
  int code = keyCode(name);
  if (code == 0) return;
  Keyboard.press((uint8_t)code);
  delay(random(40, 85));                // человекоподобное удержание
  Keyboard.releaseAll();
}

void handle(char* cmd) {
  // срезать хвостовые пробелы/CR и ведущие пробелы (без String)
  int n = (int)strlen(cmd);
  while (n > 0 && (cmd[n - 1] == ' ' || cmd[n - 1] == '\t' || cmd[n - 1] == '\r'))
    cmd[--n] = '\0';
  while (*cmd == ' ') cmd++;
  if (*cmd == '\0') return;

  if (!strcmp(cmd, "PING")) {
    Serial.println("PONG");
  } else if (!strncmp(cmd, "KEY ", 4)) {
    pressKey(cmd + 4);
  } else if (!strcmp(cmd, "CLICK")) {
    Mouse.click(MOUSE_LEFT);
  } else if (!strcmp(cmd, "RCLICK")) {
    Mouse.click(MOUSE_RIGHT);
  } else if (!strcmp(cmd, "LDOWN")) {
    Mouse.press(MOUSE_LEFT);
  } else if (!strcmp(cmd, "LUP")) {
    Mouse.release(MOUSE_LEFT);
  } else if (!strcmp(cmd, "RDOWN")) {
    Mouse.press(MOUSE_RIGHT);
  } else if (!strcmp(cmd, "RUP")) {
    Mouse.release(MOUSE_RIGHT);
  } else if (!strncmp(cmd, "MOVE ", 5)) {
    int dx, dy, ms;
    if (sscanf(cmd, "MOVE %d %d %d", &dx, &dy, &ms) == 3) smoothMove(dx, dy, ms);
  } else if (!strncmp(cmd, "DRAG ", 5)) {
    int dx, dy, ms;
    if (sscanf(cmd, "DRAG %d %d %d", &dx, &dy, &ms) == 3) {
      Mouse.press(MOUSE_RIGHT);
      delay(80);
      smoothMove(dx, dy, ms);
      delay(80);
      Mouse.release(MOUSE_RIGHT);
    } else {
      Mouse.release(MOUSE_RIGHT);
    }
  }
  Serial.println("OK");               // подтверждение выполнения (для синхронизации)
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char)Serial.read();
    if (c == '\n') {
      command[cmdLen] = '\0';
      handle(command);
      cmdLen = 0;
    } else if (c != '\r') {
      if (cmdLen < sizeof(command) - 1) command[cmdLen++] = c;
      // переполнение (строка длиннее буфера) — лишнее отбрасываем, не ломая кучу
    }
  }
}
