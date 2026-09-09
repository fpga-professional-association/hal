// ---------------------------------------------------------------------------
// accumulator_alu -- 8-bit accumulator with a 2-bit opcode.
//
// This is the *original* RTL for the walkthrough in guide.html.  Everything a
// reverse engineer is supposed to recover -- the opcode encoding, the
// subtract-as-add-of-complement trick, the zero flag, the carry flag -- is
// written out here in plain sight so the guide can be checked against it.
//
// Deliberate constraints (see spec.md):
//   * one clock domain, one asynchronous active-low reset;
//   * no vendor IP, no RAM, no DSP, no PLL, no I/O primitives, so that the
//     post-synthesis netlist contains only the cells AGILEX_TENNM.hgl models
//     (tennm_lcell_comb, tennm_ff, and the ground/power literals).
// ---------------------------------------------------------------------------

module accumulator_alu (
    input  wire       clk,
    input  wire       rst_n,     // asynchronous, active low
    input  wire [1:0] op,        // opcode, see localparams below
    input  wire [7:0] operand,   // second ALU operand
    output reg  [7:0] acc,       // the accumulator
    output reg        carry,     // registered carry-out / not-borrow flag
    output wire       zero       // combinational: acc == 0
);

    // ---- opcode encoding -------------------------------------------------
    // The reverse engineer does not get these names.  Recovering *which*
    // 2-bit pattern selects which operation is one of the exercises.
    localparam [1:0] OP_NOP = 2'b00;  // hold acc and carry
    localparam [1:0] OP_ADD = 2'b01;  // acc <= acc + operand
    localparam [1:0] OP_SUB = 2'b10;  // acc <= acc - operand
    localparam [1:0] OP_CLR = 2'b11;  // acc <= 0, carry <= 0

    // ---- the subtract trick ---------------------------------------------
    // There is exactly one adder in this design.  Subtraction reuses it as
    //     acc - operand == acc + ~operand + 1
    // so `sub` does double duty: it inverts the operand into the adder and it
    // is the carry-in of the very same chain.
    wire       sub    = (op == OP_SUB);
    wire [7:0] addend = sub ? ~operand : operand;

    wire [8:0] sum = {1'b0, acc} + {1'b0, addend} + {8'b0, sub};

    // ---- opcode decode ---------------------------------------------------
    wire       do_alu = (op == OP_ADD) || (op == OP_SUB);  // == op != 0 && op != 3
    wire       do_clr = (op == OP_CLR);

    // ---- state -----------------------------------------------------------
    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc   <= 8'h00;
            carry <= 1'b0;
        end else if (do_clr) begin
            acc   <= 8'h00;
            carry <= 1'b0;
        end else if (do_alu) begin
            acc   <= sum[7:0];
            carry <= sum[8];   // carry-out for ADD, "no borrow" for SUB
        end
        // OP_NOP: hold
    end

    // ---- flags -----------------------------------------------------------
    assign zero = ~(|acc);

endmodule
