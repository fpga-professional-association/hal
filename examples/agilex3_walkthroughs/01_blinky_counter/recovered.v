// ---------------------------------------------------------------------------
// recovered.v -- the design as reconstructed from netlist.hal.v ALONE.
//
// Written after the walkthrough in guide.html and before re-reading design.v.
// Every identifier below is either (a) a module port name, which survives
// synthesis and is therefore genuinely recovered, or (b) invented here, because
// the corresponding name is *not* recoverable in general.  See the comments.
//
// Deliberately NOT used as input: design.v, spec.md, the Quartus reports, and
// the internal net names of the netlist (`count[7]`, `add_0~81`).  Those names
// happen to be readable in this export because Quartus preserves RTL names by
// default, but a netlist that has been through an obfuscator, a third-party
// P&R flow or a bitstream-to-netlist recovery has no such gift.  The
// walkthrough therefore recovers the structure without them, and this file is
// what that structure says.
// ---------------------------------------------------------------------------

`default_nettype none

module recovered_top (
    // Recovered exactly: the module's port list survives into the .vo, and the
    // three top-level nets are unambiguous.
    //   clk    -- the only net reaching a tennm_ff.clk pin
    //   rst_n  -- the only net reaching a tennm_ff.clrn pin; active low is a
    //             property of the primitive, not a guess
    //   led    -- the only global output net
    input  wire clk,
    input  wire rst_n,
    output wire led
);

    // Width 24: the size of the single strongly connected component's register
    // set (step 3), confirmed by the 23-cell carry chain plus the one
    // normal-mode inverter cell that handles the least significant bit
    // (steps 4 and 5).
    //
    // The NAME `r` is invented.  Nothing in a gate-level netlist records that
    // the designer called this `count`.
    reg [23:0] r;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            // Reset value 0 is recovered, not assumed: `clrn` on a tennm_ff is
            // an asynchronous *clear*, so the primitive forces q to 0.  A
            // preset would have been a different pin.
            r <= 24'd0;
        end else begin
            // The increment is read out of the carry chain, one slice at a
            // time (step 5):
            //
            //   bit 0   next = !r[0]                     (normal-mode ALM)
            //   bit 1   next = r[1] ^ r[0]
            //           carry = r[1] & r[0]              (chain head, cin = 0)
            //   bit i   next = r[i] ^ cin_i
            //           carry = r[i] & cin_i             (i = 2 .. 23)
            //
            // which is exactly `r + 1`: the second operand is the constant 1,
            // folded by Quartus into "invert bit 0 and start the chain at
            // bit 1".  There is no second register bank on the chain, so this
            // is an increment and not an accumulator.
            //
            // No enable: every tennm_ff.ena is tied to the constant 1 (step 2).
            r <= r + 24'd1;
        end
    end

    // The carry out of the top slice (`add_0~1` in the export, position 22 of
    // the chain) drives nothing: its `cout` pin is unconnected.  The counter
    // therefore wraps at 2**24 rather than saturating or being cleared.
    //
    // led is bit 23 because the register at the *top* of the carry chain is
    // the one whose q net leaves the module (step 6).  That ordering comes
    // from the wiring, not from the names.
    assign led = r[23];

endmodule

`default_nettype wire
