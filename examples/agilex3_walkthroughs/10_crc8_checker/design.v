// ---------------------------------------------------------------------------
// crc8_checker -- serial CRC-8 checker, polynomial 0x07 (x^8 + x^2 + x + 1)
//
// This is the ORIGINAL design.  It is the thing a reverse engineer does not
// have: the walkthrough in guide.html starts from the synthesized netlist and
// tries to get back to something equivalent to this file.
//
// Operation
// ---------
// The classic "CRC as an LFSR with data injection" structure.  One data bit is
// consumed per enabled clock edge, MSB-first.  The remainder register `crc_r`
// is the division remainder of everything shifted in so far.
//
//   feedback = crc_r[7] ^ din          // the bit leaving the register, xored
//                                      // with the incoming message bit
//   crc_r[i] <= crc_r[i-1] ^ (feedback & POLY[i])   for i > 0
//   crc_r[0] <= feedback                            (the implicit x^0 term)
//
// POLY = 8'h07 = 8'b0000_0111, i.e. coefficients of x^2, x^1, x^0 are set
// (the x^8 term is implicit in an 8-bit CRC).  So exactly three of the eight
// next-state functions carry an XOR with the feedback bit -- bits 0, 1 and 2 --
// and the other five are a plain shift.  That asymmetry is the fingerprint the
// reverse engineer is going to look for in the netlist.
//
// Checking a codeword
// -------------------
// Shift in message||crc (MSB-first).  If the codeword is intact the remainder
// ends at zero, so `match` is the "this frame is good" flag.  Reset (`rst_n`)
// starts a new frame: init value is 0x00.
//
// Deliberate restrictions
// ---------------------
//  * one clock, no vendor IP, no RAM/DSP/PLL/IO primitives, so that the whole
//    post-synthesis netlist is inside what plugins/gate_libraries/definitions/
//    AGILEX_TENNM.hgl models (tennm_lcell_comb, tennm_ff and constants);
//  * the only asynchronous input is the active-low reset, which is what
//    tennm_ff's `clrn` models;
//  * `en` becomes the flip-flop clock enable (`ena`), the other modelled pin.
// ---------------------------------------------------------------------------

module crc8_checker (
    input  wire       clk,      // single clock domain
    input  wire       rst_n,    // asynchronous, active low: start of frame
    input  wire       en,       // consume one serial bit this cycle
    input  wire       din,      // serial data, MSB first
    output wire [7:0] crc,      // running remainder
    output wire       match     // remainder == 0  ->  codeword accepted
);

    // CRC-8 polynomial, low 8 coefficients.  x^8 is implicit.
    localparam [7:0] POLY = 8'h07;

    reg [7:0] crc_r;

    // The bit that falls out of the top of the register, xored with the
    // incoming message bit.  This single wire fans out to every tapped stage.
    wire feedback = crc_r[7] ^ din;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            crc_r <= 8'h00;
        end else if (en) begin
            crc_r[0] <= feedback;                        // x^0 term, always set
            crc_r[1] <= crc_r[0] ^ (feedback & POLY[1]); // POLY[1] = 1 -> tapped
            crc_r[2] <= crc_r[1] ^ (feedback & POLY[2]); // POLY[2] = 1 -> tapped
            crc_r[3] <= crc_r[2] ^ (feedback & POLY[3]); // POLY[3] = 0 -> shift
            crc_r[4] <= crc_r[3] ^ (feedback & POLY[4]); // POLY[4] = 0 -> shift
            crc_r[5] <= crc_r[4] ^ (feedback & POLY[5]); // POLY[5] = 0 -> shift
            crc_r[6] <= crc_r[5] ^ (feedback & POLY[6]); // POLY[6] = 0 -> shift
            crc_r[7] <= crc_r[6] ^ (feedback & POLY[7]); // POLY[7] = 0 -> shift
        end
    end

    assign crc   = crc_r;
    assign match = (crc_r == 8'h00);

endmodule
