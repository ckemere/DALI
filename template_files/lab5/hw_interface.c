
#include <hw_interface.h>


// ====================================================================================================================
// ====================================================================================================================
/* TODO - write these functions! */

void SetTimerA1Period(uint16_t period) {

    // HINT: This function should change both the PWM period
    //   AND the PWM duty cycle to be 50% of the period!!!
    //   It probably would be useful to #define the set of periods that correspond to the tones you  want to use!
    //   Those sorts of constants should go in the HEADER file!

    return;
}

void EnableTimerA1PWM(void) {
    // Hint: This function just needs to toggle 1 bit in a register!
    return;
}

void DisableTimerA1PWM(void) {
    // Hint: This function just needs to toggle 1 bit in a register!
    return;
}
// ====================================================================================================================
// ====================================================================================================================

void delay_cycles(uint32_t cycles)
{
    /* this is a scratch register for the compiler to use */
    uint32_t scratch;

    /* There will be a 2 cycle delay here to fetch & decode instructions
     * if branch and linking to this function */

    /* Subtract 2 net cycles for constant offset: +2 cycles for entry jump,
     * +2 cycles for exit, -1 cycle for a shorter loop cycle on the last loop,
     * -1 for this instruction */

    __asm volatile(
#ifdef __GNUC__
        ".syntax unified\n\t"
#endif
        "SUBS %0, %[numCycles], #2; \n"
        "%=: \n\t"
        "SUBS %0, %0, #4; \n\t"
        "NOP; \n\t"
        "BHS  %=b;" /* branches back to the label defined above if number > 0 */
        /* Return: 2 cycles */
        : "=&r"(scratch)
        : [ numCycles ] "r"(cycles));
}


void InitializeGPIO(void) {
    GPIOA->GPRCM.RSTCTL = (GPIO_RSTCTL_KEY_UNLOCK_W | GPIO_RSTCTL_RESETSTKYCLR_CLR | GPIO_RSTCTL_RESETASSERT_ASSERT);
    GPIOA->GPRCM.PWREN = (GPIO_PWREN_KEY_UNLOCK_W | GPIO_PWREN_ENABLE_ENABLE);

    delay_cycles(POWER_STARTUP_DELAY); // delay to enable GPIO to turn on and reset

    // ===============================================================================================================
    // 1. Initialize IOMUX for PWM!!
    IOMUX->SECCFG.PINCM[(IOMUX_PINCM37)] = IOMUX_PINCM_PC_CONNECTED | IOMUX_PINCM37_PF_TIMA1_CCP0; // TIMA1-CC0 on PA15
    // ===============================================================================================================


    // ===============================================================================================================
    // Initialize IOMUX for Button inputs
    // We have to do this multiple times, so let's define it once and then re-use the code
    const uint32_t input_configuration = IOMUX_PINCM_PC_CONNECTED | IOMUX_PINCM_INENA_ENABLE |
            ((uint32_t) 0x00000001) | // GPIO function is always MUX entry 1
            IOMUX_PINCM_INV_DISABLE | // TODO: experiment with setting this to invert our logic
            IOMUX_PINCM_PIPU_ENABLE | IOMUX_PINCM_PIPD_DISABLE | // pull up resistor - switch connects to ground
            IOMUX_PINCM_HYSTEN_DISABLE | // disable input pin hysteresis (TODO: experiment with this)
            IOMUX_PINCM_WUEN_DISABLE;  // wake-up disable (TODO: experiment with this for ultra low power!)

    IOMUX->SECCFG.PINCM[(IOMUX_PINCM53)] = input_configuration; // PA23
    IOMUX->SECCFG.PINCM[(IOMUX_PINCM54)] = input_configuration; // PA24
    IOMUX->SECCFG.PINCM[(IOMUX_PINCM55)] = input_configuration; // PA25
    IOMUX->SECCFG.PINCM[(IOMUX_PINCM59)] = input_configuration; // PA26
    // ===============================================================================================================

    delay_cycles(POWER_STARTUP_DELAY); // delay to enable GPIO to turn on and reset
}


inline void SetTimerG0Delay(uint16_t delay) {
    TIMG0->COUNTERREGS.LOAD = delay; // This will load as soon as timer is enabled
}

inline void EnableTimerG0() {
    TIMG0->COUNTERREGS.CTRCTL |= (GPTIMER_CTRCTL_EN_ENABLED);
    NVIC_EnableIRQ(TIMG0_INT_IRQn); // Enable the timer interrupt
}

void InitializeTimerG0() {

    /* Timer module and Sleep Mode Initialization */
    // 1. Step 1 is always to reset and enable
    TIMG0->GPRCM.RSTCTL = (GPTIMER_RSTCTL_KEY_UNLOCK_W | GPTIMER_RSTCTL_RESETSTKYCLR_CLR | GPTIMER_RSTCTL_RESETASSERT_ASSERT);
    TIMG0->GPRCM.PWREN = (GPTIMER_PWREN_KEY_UNLOCK_W | GPTIMER_PWREN_ENABLE_ENABLE);
    delay_cycles(16); // delay to enable module to turn on and reset

    // 2. Step 2 is to choose the desired clock. We want UPCLK (LFCLK) so we can use a LPM
    // TIMG0->CLKSEL = GPTIMER_CLKSEL_BUSCLK_SEL_ENABLE;
    TIMG0->CLKSEL = GPTIMER_CLKSEL_LFCLK_SEL_ENABLE;

    // 3. By default, the timer counts down to zero and then stops. Configure it to repeat.
    TIMG0->COUNTERREGS.CTRCTL = GPTIMER_CTRCTL_REPEAT_REPEAT_1;

    // 4. Enable timer interrupt when we reach 0
    TIMG0->CPU_INT.IMASK |= GPTIMER_CPU_INT_IMASK_Z_SET;

    // 5. Enable the clock input to the timer. (The timer itself is still not enabled!)
    TIMG0->COMMONREGS.CCLKCTL = GPTIMER_CCLKCTL_CLKEN_ENABLED;
    
}

// The function needs to be put into the interrupt table!!!!
void TIMG0_IRQHandler(void)
{
    // This wakes up the processor!
    switch (TIMG0->CPU_INT.IIDX) {
        case GPTIMER_CPU_INT_IIDX_STAT_Z: // Counted down to zero event.
            // If we wanted to execute code in the ISR, it would go here.
            break;
        default:
            break;
    }
}

void InitializeTimerA1_PWM(void) {
    TIMA1->GPRCM.RSTCTL = (GPTIMER_RSTCTL_KEY_UNLOCK_W | GPTIMER_RSTCTL_RESETSTKYCLR_CLR | GPTIMER_RSTCTL_RESETASSERT_ASSERT);
    TIMA1->GPRCM.PWREN = (GPTIMER_PWREN_KEY_UNLOCK_W | GPTIMER_PWREN_ENABLE_ENABLE);
    delay_cycles(POWER_STARTUP_DELAY); // delay to enable module to turn on and reset

    // Configure clocking for Timer Module
    TIMA1->CLKSEL = GPTIMER_CLKSEL_BUSCLK_SEL_ENABLE; // BUSCLK will be SYSOSC / 32 MHz
    TIMA1->CLKDIV = GPTIMER_CLKDIV_RATIO_DIV_BY_4; // Divide by 4 to make PWM clock frequency 8 MHz

    TIMA1->COUNTERREGS.CCACT_01[0] = GPTIMER_CCACT_01_ZACT_CCP_HIGH | GPTIMER_CCACT_01_CUACT_CCP_LOW;
    TIMA1->COUNTERREGS.CTRCTL = GPTIMER_CTRCTL_REPEAT_REPEAT_1 | GPTIMER_CTRCTL_CM_UP |
            GPTIMER_CTRCTL_CVAE_ZEROVAL | GPTIMER_CTRCTL_EN_DISABLED;
    TIMA1->COMMONREGS.CCPD = GPTIMER_CCPD_C0CCP0_OUTPUT | GPTIMER_CCPD_C0CCP1_OUTPUT;
    TIMA1->COMMONREGS.CCLKCTL = (GPTIMER_CCLKCTL_CLKEN_ENABLED);

    TIMA1->COUNTERREGS.LOAD = 3999; // Period is LOAD+1 - this is 8000000/4000 = 2kHz
    // HEADS UP: This sets the frequency of the buzzer!

    TIMA1->COUNTERREGS.CC_01[0] = (TIMA1->COUNTERREGS.LOAD  + 1) / 2; // half of load to make this a square wave
    TIMA1->COUNTERREGS.CTRCTL |= (GPTIMER_CTRCTL_EN_ENABLED);

}

/*
 *
 * The delay function is a reproduction of standard TI code
 * Copyright (c) 2020, Texas Instruments Incorporated
 * All rights reserved.
 *
 * All other code is Copyright (c) 2026, Caleb Kemere
 * All rights reserved, see LICENSE.md
 *
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 *
 * *  Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 *
 * *  Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 *
 * *  Neither the name of Texas Instruments Incorporated nor the names of
 *    its contributors may be used to endorse or promote products derived
 *    from this software without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
 * AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO,
 * THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
 * PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT OWNER OR
 * CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL,
 * EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO,
 * PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS;
 * OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY,
 * WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR
 * OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE,
 * EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
 *
 */

