
/*
 * Copyright (c) 2026, Caleb Kemere
 * All rights reserved, see LICENSE.md
 *
 */

#include <ti/devices/msp/msp.h>
#include "hw_interface.h"

int main(void)
{
    InitializeGPIO();

    InitializeTimerG0();
    InitializeTimerA1_PWM();

    // let the buzzer run for 0.1 s just so we know it's there!
    delay_cycles(1600000);
    TIMA1->COUNTERREGS.CTRCTL &= ~(GPTIMER_CTRCTL_EN_ENABLED); // Disable the buzzer

    SetTimerG0Delay(20); // 20 ticks at 32 kHz is 0.6 ms
    EnableTimerG0();

    // VERY BASIC LOOP - If button 1 signals a 0, enable the PWM
    while (1) {
        uint32_t input = GPIOA->DIN31_0 & (SW1 + SW2 + SW3 + SW4);
        if ((input & SW1) == 0) { // active low!
            TIMA1->COUNTERREGS.CTRCTL |= (GPTIMER_CTRCTL_EN_ENABLED); // Enable the buzzer
        }
        else {
            TIMA1->COUNTERREGS.CTRCTL &= ~(GPTIMER_CTRCTL_EN_ENABLED); // Disable the buzzer
        }

        // The above is just a basic example I expect you to implement functions that look something like this:
        // SetPWMPeriodAndEnablement(state);
        // state = GetNextState(state, input);

        __WFI(); // Go to sleep until timer counts down again.
    }

}


