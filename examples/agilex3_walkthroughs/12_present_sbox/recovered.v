// -----------------------------------------------------------------------------
// present_sbox_recovered -- the RTL as reconstructed from present_sbox.vo alone.
//
// Written from the analysis in guide.html, not from design.v.  Every constant
// below is traceable to a step of `analysis.py`:
//
//   SBOX     the 16-entry table read out of the 4-input LUT cones (step 2);
//            17 groups, 16 of them over the datapath register and one over the
//            key register, all the same table.
//   PLAYER   which flip-flop each substitution output drives (step 3), matched
//            functionally, cone against cone, never by net name.
//   31       the number of cycles the design stays busy, and the last value the
//            5-bit counter takes (step 4).
//   61 / 76 / 19:15
//            the key-schedule rotation, the substituted nibble and the counter
//            injection, read off the key register's own next-state wiring.
//
// The port names, the module name and the signal names are ours; the export
// carries the original ones, and walkthrough 16 is where that convenience goes
// away.  What is *not* ours is the placement of the register boundary: the
// substitution layer reads the register directly, so the value in the register
// is the state after the round key has been added.  The reconstruction keeps
// that, because it is what the netlist does.
//
// Not recovered, because it is not in the netlist: decryption.
// -----------------------------------------------------------------------------

module present_sbox_recovered (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    input  wire [63:0] plaintext,
    input  wire [79:0] key_in,
    output wire [63:0] ciphertext,
    output wire        busy,
    output wire        done
);

    localparam [4:0] ROUNDS = 5'd31;

    function [3:0] sbox;
        input [3:0] x;
        begin
            case (x)
                4'h0: sbox = 4'hC;
                4'h1: sbox = 4'h5;
                4'h2: sbox = 4'h6;
                4'h3: sbox = 4'hB;
                4'h4: sbox = 4'h9;
                4'h5: sbox = 4'h0;
                4'h6: sbox = 4'hA;
                4'h7: sbox = 4'hD;
                4'h8: sbox = 4'h3;
                4'h9: sbox = 4'hE;
                4'hA: sbox = 4'hF;
                4'hB: sbox = 4'h8;
                4'hC: sbox = 4'h4;
                4'hD: sbox = 4'h7;
                4'hE: sbox = 4'h1;
                default: sbox = 4'h2;
            endcase
        end
    endfunction

    reg [63:0] datapath;
    reg [79:0] keyreg;
    reg [4:0]  counter;
    reg        active;
    reg        finished;

    wire load = start & ~active;
    wire last = active & (counter == ROUNDS);

    wire [63:0] subs /* synthesis keep */;

    genvar i;
    generate
        for (i = 0; i < 16; i = i + 1) begin : substitution
            assign subs[4*i+3 : 4*i] = sbox(datapath[4*i+3 : 4*i]);
        end
    endgenerate

    wire [63:0] perm;

    generate
        for (i = 0; i < 64; i = i + 1) begin : permutation
            assign perm[(i == 63) ? 63 : (16 * i) % 63] = subs[i];
        end
    endgenerate

    wire [79:0] krot = {keyreg[18:0], keyreg[79:19]};
    wire [3:0]  ksub /* synthesis keep */;

    assign ksub = sbox(krot[79:76]);

    wire [79:0] knext = {ksub, krot[75:20], krot[19:15] ^ counter, krot[14:0]};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            datapath <= 64'b0;
            keyreg   <= 80'b0;
            counter  <= 5'b0;
            active   <= 1'b0;
        end else if (load) begin
            datapath <= plaintext ^ key_in[79:16];
            keyreg   <= key_in;
            counter  <= 5'd1;
            active   <= 1'b1;
        end else if (active) begin
            datapath <= perm ^ knext[79:16];
            keyreg   <= knext;
            counter  <= last ? 5'd0 : counter + 5'd1;
            active   <= ~last;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            finished <= 1'b0;
        else if (load | active)
            finished <= last;
    end

    assign ciphertext = datapath;
    assign busy       = active;
    assign done       = finished;

endmodule
