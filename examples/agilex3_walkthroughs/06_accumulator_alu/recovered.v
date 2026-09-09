// ---------------------------------------------------------------------------
// recovered.v -- the design as reverse-engineered from netlist.hal.v alone.
//
// Written while looking ONLY at the netlist and at the artifacts/ produced by
// analyze.py.  design.v was not consulted while deriving it; the comparison
// against design.v is in guide.html, section (e).
//
// Two modules on purpose:
//
//   recovered_literal   a one-for-one transcription of the netlist.  Every
//                       signal here corresponds to a cell or a bundle of
//                       cells in the export, and the odd shapes (the AND with
//                       `keep`, the enable term) are the netlist's, not a
//                       designer's.
//
//   recovered_clean     the same behaviour rewritten the way a human would
//                       have written it, once the opcode encoding is known.
//                       This is the actual claim about the original design.
//
// Both are checked against the exported netlist by check.py.
// ---------------------------------------------------------------------------


// === literal transcription =================================================
//
// Netlist evidence for each line is in artifacts/02_carry_chain.txt and
// artifacts/04_control_cone.txt.

module recovered_literal (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [1:0] op,
    input  wire [7:0] operand,
    output wire [7:0] acc,
    output wire       carry,
    output wire       zero
);

    reg [7:0] acc_r;
    reg       carry_r;

    // Cell `add_0~47`: an arithmetic ALM that drives only `cout`.  It has no
    // `sumout` consumer, so it is not a bit slice -- it is the carry-in
    // generator of the chain.  Its equation is  cout = !op[0] & op[1].
    wire sub = (~op[0]) & op[1];

    // Cells `add_0~1 .. add_0~36`: eight identical arithmetic slices, all with
    // lut_mask 0x00000000002D2DD2, wired dataa=~op[0] datab=~op[1]
    // datac=~operand[i] datad=~acc[i].  Their propagate half evaluates to
    //     operand[i] ^ acc[i] ^ sub
    // and their generate half to the majority of the same three terms, i.e.
    // the operand is conditionally inverted *inside* the slice.
    wire [7:0] b = operand ^ {8{sub}};

    // The chain is 8 slices plus a 10th cell `add_0~41` with lut_mask 0 whose
    // equation is  sumout = cin , i.e. a pure tap on the carry out of bit 7.
    wire [8:0] sum = {1'b0, acc_r} + {1'b0, b} + {8'b0, sub};

    // Cell `i45~1` drives the `ena` pin of all nine flip-flops:
    //     ena = op[0] | op[1]
    wire enable = op[0] | op[1];

    // Cells `i45~0, i45~2 .. i45~8` drive the eight `d` pins and `i46~0`
    // drives the carry flip-flop's `d`.  All ten have the same shape:
    //     d = <sum bit> & ((!op[0]) | (!op[1]))
    wire keep = (~op[0]) | (~op[1]);

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc_r   <= 8'h00;   // tennm_ff.clrn = rst_n, asynchronous, active low
            carry_r <= 1'b0;
        end else if (enable) begin
            acc_r   <= sum[7:0] & {8{keep}};
            carry_r <= sum[8]   &     keep;
        end
    end

    assign acc   = acc_r;
    assign carry = carry_r;

    // Cells `reduce_or_0~0` (NOR of acc[3:0]) and `reduce_or_0` (that result
    // ANDed with the NOR of acc[7:4]).  A two-level 8-input NOR of the
    // accumulator outputs.
    assign zero = ~(|acc_r);

endmodule


// === cleaned up ============================================================
//
// `enable` and `keep` between them are a decode of `op`:
//     op == 2'b00 -> enable = 0                     hold
//     op == 2'b11 -> enable = 1, keep = 0           write zero
//     otherwise   -> enable = 1, keep = 1           write the sum
// and `sub` is exactly op == 2'b10.  That is a four-entry opcode table.

module recovered_clean (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [1:0] op,
    input  wire [7:0] operand,
    output reg  [7:0] acc,
    output reg        carry,
    output wire       zero
);

    localparam [1:0] OP_NOP = 2'b00;
    localparam [1:0] OP_ADD = 2'b01;
    localparam [1:0] OP_SUB = 2'b10;
    localparam [1:0] OP_CLR = 2'b11;

    wire       sub    = (op == OP_SUB);
    wire [7:0] addend = sub ? ~operand : operand;
    wire [8:0] sum    = {1'b0, acc} + {1'b0, addend} + {8'b0, sub};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            acc   <= 8'h00;
            carry <= 1'b0;
        end else begin
            case (op)
                OP_NOP: ;                                   // hold
                OP_CLR: begin acc <= 8'h00; carry <= 1'b0; end
                default: begin acc <= sum[7:0]; carry <= sum[8]; end
            endcase
        end
    end

    assign zero = ~(|acc);

endmodule
