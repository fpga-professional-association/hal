// Textbook-placement variant of present_sbox: the register holds state_i
// (before the round key is added) and nothing is marked `keep`.  Functionally
// the same PRESENT-80 encryption; structurally the S-box layer no longer
// exists as a signal.
module present_sbox_textbook (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        start,
    input  wire [63:0] plaintext,
    input  wire [79:0] key_in,
    output wire [63:0] ciphertext,
    output wire        busy,
    output wire        done
);

    // 31 rounds plus one extra cycle for the final key addition, which in this
    // placement has nowhere else to go.
    localparam [5:0] ROUNDS = 6'd32;

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

    reg [63:0] state;
    reg [79:0] kreg;
    reg [5:0]  round;
    reg        running;
    reg        done_r;

    wire load = start & ~running;
    wire last = running & (round == ROUNDS);

    wire [63:0] added = state ^ kreg[79:16];
    wire [63:0] subs;

    genvar i;
    generate
        for (i = 0; i < 16; i = i + 1) begin : sbox_layer
            assign subs[4*i+3 : 4*i] = sbox(added[4*i+3 : 4*i]);
        end
    endgenerate

    wire [63:0] perm;
    generate
        for (i = 0; i < 64; i = i + 1) begin : p_layer
            assign perm[(i == 63) ? 63 : (16 * i) % 63] = subs[i];
        end
    endgenerate

    wire [79:0] krot = {kreg[18:0], kreg[79:19]};
    wire [3:0]  ksub = sbox(krot[79:76]);
    wire [79:0] knext = {ksub, krot[75:20], krot[19:15] ^ round[4:0], krot[14:0]};

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state   <= 64'b0;
            kreg    <= 80'b0;
            round   <= 6'b0;
            running <= 1'b0;
        end else if (load) begin
            state   <= plaintext;
            kreg    <= key_in;
            round   <= 6'd1;
            running <= 1'b1;
        end else if (running) begin
            state   <= last ? added : perm;
            kreg    <= knext;
            round   <= last ? 6'd0 : round + 6'd1;
            running <= ~last;
        end
    end

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n)
            done_r <= 1'b0;
        else if (load | running)
            done_r <= last;
    end

    assign ciphertext = state;
    assign busy       = running;
    assign done       = done_r;

endmodule
