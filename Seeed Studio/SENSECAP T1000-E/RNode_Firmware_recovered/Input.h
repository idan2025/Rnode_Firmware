#line 1 "/tmp/RNode_Firmware/Input.h"
// Copyright (C) 2024, Mark Qvist

// This program is free software: you can redistribute it and/or modify
// it under the terms of the GNU General Public License as published by
// the Free Software Foundation, either version 3 of the License, or
// (at your option) any later version.

// This program is distributed in the hope that it will be useful,
// but WITHOUT ANY WARRANTY; without even the implied warranty of
// MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
// GNU General Public License for more details.

// You should have received a copy of the GNU General Public License
// along with this program.  If not, see <https://www.gnu.org/licenses/>.

#ifndef INPUT_H
  #define INPUT_H
  
  #define PIN_BUTTON pin_btn_usr1

  // T1000-E wires its user button active-HIGH with an external/internal
  // pull-down (confirmed against Meshtastic's variant.h for this exact
  // board: BUTTON_ACTIVE_LOW=false, BUTTON_ACTIVE_PULLUP=false, pull-down
  // sense type - see Boards.h). Every other supported board here is
  // active-LOW with an internal pull-up, which is why PRESSED/RELEASED
  // were hardcoded that way before BUTTON_ACTIVE_LOW existed. Without this
  // override, digitalRead(PIN_BUTTON) never toggles on a T1000-E (idle-HIGH
  // from the pull-up, driven-HIGH on press too), so input_read() never
  // detects a state change and button_event() is never called at all, for
  // any hold duration.
  #if BUTTON_ACTIVE_LOW
    #define PRESSED LOW
    #define RELEASED HIGH
  #else
    // Board has an external pull-down: idle reads LOW, pressed reads HIGH.
    #define PRESSED HIGH
    #define RELEASED LOW
  #endif

  #define EVENT_ALL                 0x00
  #define EVENT_CLICKS              0x01
  #define EVENT_BUTTON_DOWN         0x11
  #define EVENT_BUTTON_UP           0x12
  #define EVENT_BUTTON_CLICK        0x13
  #define EVENT_BUTTON_DOUBLE_CLICK 0x14
  #define EVENT_BUTTON_TRIPLE_CLICK 0x15
  
  int button_events = EVENT_CLICKS;
  int button_state = RELEASED;
  int debounce_state = button_state;
  unsigned long button_debounce_last = 0;
  unsigned long button_debounce_delay = 25;
  unsigned long button_down_last = 0;
  unsigned long button_up_last = 0;

  // Forward declaration
  void button_event(uint8_t event, unsigned long duration);

  void input_init() {
    #if BUTTON_ACTIVE_LOW
      pinMode(PIN_BUTTON, INPUT_PULLUP);
    #else
      pinMode(PIN_BUTTON, INPUT_PULLDOWN);
    #endif
  }

  void input_get_all_events() {
    button_events = EVENT_ALL;
  }

  void input_get_click_events() {
    button_events = EVENT_CLICKS;
  }

  void input_read() {
    int button_reading = digitalRead(PIN_BUTTON);
    if (button_reading != debounce_state) {
      button_debounce_last = millis();
      debounce_state = button_reading;
    }

    if ((millis() - button_debounce_last) > button_debounce_delay) {
      if (button_reading != button_state) {
        // State changed
        int previous_state = button_state;
        button_state = button_reading;

        if (button_events == EVENT_ALL) {
          if (button_state == PRESSED) {
            button_event(EVENT_BUTTON_DOWN, 0);
          } else if (button_state == RELEASED) {
            button_event(EVENT_BUTTON_UP, 0);
          }
        } else if (button_events == EVENT_CLICKS) {
          if (previous_state == PRESSED && button_state == RELEASED) {
            button_up_last = millis();
            button_event(EVENT_BUTTON_CLICK, button_up_last-button_down_last);
          } else if (previous_state == RELEASED && button_state == PRESSED) {
            button_down_last = millis();
          }
        }
      }
    }

  }

  bool button_pressed() {
    if (button_state == PRESSED) {
      return true;
    } else {
      return false;
    }
  }

#endif